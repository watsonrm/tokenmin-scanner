#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Tokenmin detector-research synthesis stage (Claude-judged).

Stage 2 of the two-stage detector-research workflow:

    Stage 1 (bin/detector-research.py) — pure URL discovery. Diffs against
    bin/.research-seen.json, writes the fresh-URL list to bin/.research-fresh.json,
    commits the updated seen state. Does not file any per-URL issues (that was
    the old behavior — see --legacy-file-issues on the stage-1 script).

    Stage 2 (this file) — semantic judgment. For each fresh URL:
      1. Fetch the page (capped at MAX_FETCH_CHARS, default 50K).
      2. Call the Anthropic API with the prompt from bin/synthesis-prompt.md.
      3. Parse the JSON verdict.
      4. If candidate=true: file a properly-formatted research-candidate issue.
      5. Append the URL + verdict to a weekly digest issue (one per ISO week).

Why a separate stage:
  - Cost discipline. The fetch-and-judge work only runs for URLs we haven't
    judged before. Idempotent re-runs cost zero (fresh list is empty).
  - Prompt iteration. bin/synthesis-prompt.md lives next to the script; edit
    the prompt, push, next cron picks it up. No code changes needed.
  - Failure isolation. A bad API key, exhausted cost cap, or single-URL
    fetch failure doesn't kill the whole pipeline — stage 1 still committed.

Cost-cap math (default $1.00/run):
  - Sonnet 4.6 pricing: $3/MTok input, $15/MTok output (as of 2026-05).
  - Typical synthesis call: ~12K input tokens (prompt + page) + ~300 output
    tokens (JSON verdict) ≈ $0.041/call.
  - $1.00 cap → ~24 URLs per run before stop, then the rest are deferred to
    the next cron with a note in the digest issue.
  - Haiku 4.5 (cheaper, set MODEL=claude-haiku-4-5): ~$0.005/call → ~200/run
    under the same cap.
  - Cost cap is a SOFT ceiling: we stop calling the API the moment cumulative
    usage exceeds it. Already-filed candidates are not rolled back.

Stdlib only (matches the stage-1 script style). HTTP-POST direct to the
Anthropic Messages API; no `anthropic` Python SDK install needed in CI.

Environment:
  - ANTHROPIC_API_KEY     required; the secret Rick has to add to the repo
  - GITHUB_REPOSITORY     auto-set by GitHub Actions
  - GH_TOKEN              for `gh` CLI calls
  - TOKENMIN_SYNTH_MODEL  optional override; default 'claude-sonnet-4-6'
  - TOKENMIN_SYNTH_BUDGET optional USD cap as float; default 1.00
  - TOKENMIN_SYNTH_MAX_FETCH_CHARS optional; default 50_000
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


BIN_DIR = Path(__file__).resolve().parent
FRESH_PATH = BIN_DIR / ".research-fresh.json"
PROMPT_PATH = BIN_DIR / "synthesis-prompt.md"

DEFAULT_MODEL = "claude-sonnet-4-6"
DEFAULT_BUDGET_USD = 1.00
DEFAULT_MAX_FETCH_CHARS = 50_000
USER_AGENT = "tokenmin-detector-synthesize/1.0 (+https://github.com/watsonrm/tokenmin-scanner)"
TIMEOUT_SEC = 30

ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"

# Per-million-token USD pricing. Used only for the cost cap; if a model isn't
# listed we fall back to Sonnet's rates (worst case = stop sooner).
PRICING_PER_MTOK = {
    "claude-sonnet-4-6":  (3.00, 15.00),
    "claude-sonnet-4-7":  (3.00, 15.00),
    "claude-opus-4-6":    (15.00, 75.00),
    "claude-opus-4-7":    (15.00, 75.00),
    "claude-haiku-4-5":   (0.80, 4.00),
}


# ----- HTTP helpers ---------------------------------------------------------

def _fetch_url_content(url: str, max_chars: int) -> str:
    """GET a URL and return its body truncated to max_chars. HTML stays as-is —
    Claude does fine reading raw HTML, no need to strip tags here.
    """
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml,text/plain;q=0.9,*/*;q=0.8",
        },
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT_SEC) as r:
        raw = r.read(max_chars * 4)  # over-read; one char can be multiple bytes
    body = raw.decode("utf-8", errors="replace")
    if len(body) > max_chars:
        body = body[:max_chars] + "\n\n[...truncated by detector-synthesize...]"
    return body


def _call_anthropic(api_key: str, model: str, system: str, user_msg: str) -> dict:
    """POST to /v1/messages. Returns parsed JSON response.

    Sends `system` as the system prompt (cacheable on Anthropic's side; we
    set cache_control on it so multi-URL runs only pay full input rate once).
    """
    payload = {
        "model": model,
        "max_tokens": 1024,
        "system": [
            {
                "type": "text",
                "text": system,
                "cache_control": {"type": "ephemeral"},
            },
        ],
        "messages": [
            {"role": "user", "content": user_msg},
        ],
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        ANTHROPIC_API_URL,
        data=data,
        headers={
            "Content-Type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": ANTHROPIC_VERSION,
            "User-Agent": USER_AGENT,
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode("utf-8"))


def _verdict_from_response(resp: dict) -> dict:
    """Parse the {candidate: bool, ...} JSON object out of the model's reply.

    The prompt instructs strict JSON. We still defensively strip markdown fence
    wrappers in case the model slips up.
    """
    content_blocks = resp.get("content", [])
    text_blob = "".join(b.get("text", "") for b in content_blocks if b.get("type") == "text").strip()
    # Strip ```json ... ``` fences if present.
    text_blob = re.sub(r"^```(?:json)?\s*", "", text_blob)
    text_blob = re.sub(r"\s*```$", "", text_blob)
    return json.loads(text_blob)


def _usage_cost_usd(model: str, usage: dict) -> float:
    """Compute USD cost from the Anthropic usage block.

    Counts cache_creation as input (1.25x), cache_read as input (0.1x); rest at
    full input rate. Output at output rate.
    """
    in_rate, out_rate = PRICING_PER_MTOK.get(model, PRICING_PER_MTOK[DEFAULT_MODEL])
    input_tok = usage.get("input_tokens", 0)
    cache_creation = usage.get("cache_creation_input_tokens", 0)
    cache_read = usage.get("cache_read_input_tokens", 0)
    output_tok = usage.get("output_tokens", 0)
    cost = (
        input_tok        * in_rate         / 1_000_000
        + cache_creation * (in_rate * 1.25) / 1_000_000
        + cache_read     * (in_rate * 0.10) / 1_000_000
        + output_tok     * out_rate         / 1_000_000
    )
    return cost


# ----- GitHub issue helpers -------------------------------------------------

def _file_candidate_issue(
    repo: str,
    url: str,
    feed: str,
    model: str,
    cost_usd: float,
    verdict: dict,
) -> str | None:
    """File a research-candidate issue matching the scanner#15 entry shape.

    Returns the issue URL on success, None on failure.
    """
    title = f"Detector candidate: {verdict.get('title', '(no title)')[:160]}"
    pillar = verdict.get("pillar", "?")
    tier = verdict.get("tier", "?")
    body = "\n".join([
        f"**Source URL:** {url}",
        f"**Source feed:** {feed}",
        f"**Detected by:** Claude synthesis pass (auto-filed)",
        f"**Model:** {model} · cost: ${cost_usd:.4f}",
        "",
        "### Pattern",
        "",
        verdict.get("pattern", "(none)"),
        "",
        "### Signal",
        "",
        verdict.get("signal", "(none)"),
        "",
        "### Evidence (verbatim quote from the source)",
        "",
        "> " + verdict.get("evidence_quote", "(none)").replace("\n", "\n> "),
        "",
        f"### Pillar / Tier",
        "",
        f"Pillar {pillar} / Tier {tier}",
        "",
        "### Triage",
        "",
        "- [ ] Validate the signal is derivable from current snapshot schema (may need extension)",
        "- [ ] Verify the cited evidence still exists at the source URL",
        "- [ ] Decide: implement / defer / drop",
        "- [ ] If implement: file follow-up issue with detector design + tests",
    ])
    schema_ext = verdict.get("schema_extension_needed", "").strip()
    if schema_ext:
        body += f"\n\n_Claude flagged a schema extension may be needed: `{schema_ext}`_"
    try:
        r = subprocess.run(
            ["gh", "issue", "create",
             "--repo", repo,
             "--title", title,
             "--body", body,
             "--label", "research-candidate"],
            check=True, capture_output=True, text=True, timeout=30,
        )
        out_url = r.stdout.strip().splitlines()[-1] if r.stdout.strip() else ""
        print(f"  + candidate filed: {title[:80]} → {out_url}")
        return out_url
    except subprocess.CalledProcessError as exc:
        print(f"  ! gh issue create failed: {exc.stderr}", file=sys.stderr)
        return None


def _find_or_create_digest(repo: str, iso_week_date: str) -> int | None:
    """Find an open digest issue for the current week, or create one.

    Returns the issue NUMBER (not URL) so we can later `gh issue edit`.
    """
    digest_title = f"Detector research digest — {iso_week_date}"
    try:
        r = subprocess.run(
            ["gh", "issue", "list",
             "--repo", repo,
             "--state", "open",
             "--search", f'"{digest_title}" in:title',
             "--json", "number,title",
             "--limit", "5"],
            check=True, capture_output=True, text=True, timeout=30,
        )
        items = json.loads(r.stdout or "[]")
        for it in items:
            if it.get("title") == digest_title:
                return it.get("number")
    except subprocess.CalledProcessError as exc:
        print(f"  ! gh issue list failed: {exc.stderr}", file=sys.stderr)
        # Fall through and try to create — duplicate is acceptable, better than dropping the digest.

    # Create
    body = (
        f"_Auto-maintained by `bin/detector-synthesize.py`. Edited in place as the "
        f"weekly cron runs; each row is one URL examined by the Claude synthesis pass._\n\n"
        f"| URL | Source | Verdict | Candidate filed |\n"
        f"|---|---|---|---|\n"
    )
    try:
        r = subprocess.run(
            ["gh", "issue", "create",
             "--repo", repo,
             "--title", digest_title,
             "--body", body,
             "--label", "research-candidate"],
            check=True, capture_output=True, text=True, timeout=30,
        )
        # gh outputs the URL on the last line; extract the issue number.
        url = r.stdout.strip().splitlines()[-1]
        m = re.search(r"/issues/(\d+)$", url)
        if m:
            return int(m.group(1))
    except subprocess.CalledProcessError as exc:
        print(f"  ! gh issue create (digest) failed: {exc.stderr}", file=sys.stderr)
    return None


def _append_digest_row(repo: str, issue_number: int, row: str) -> None:
    """Read the digest issue body, append one row, write it back."""
    try:
        r = subprocess.run(
            ["gh", "issue", "view", str(issue_number),
             "--repo", repo, "--json", "body"],
            check=True, capture_output=True, text=True, timeout=30,
        )
        body = json.loads(r.stdout).get("body", "")
    except subprocess.CalledProcessError as exc:
        print(f"  ! gh issue view (digest) failed: {exc.stderr}", file=sys.stderr)
        return

    new_body = body.rstrip() + "\n" + row + "\n"
    try:
        subprocess.run(
            ["gh", "issue", "edit", str(issue_number),
             "--repo", repo, "--body", new_body],
            check=True, capture_output=True, text=True, timeout=30,
        )
    except subprocess.CalledProcessError as exc:
        print(f"  ! gh issue edit (digest) failed: {exc.stderr}", file=sys.stderr)


# ----- Main -----------------------------------------------------------------

def main() -> int:
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        print("detector-synthesize: ANTHROPIC_API_KEY not set; nothing to do.")
        # Exit 0 — this should not fail the whole workflow during the staged
        # rollout (Rick adds the secret before this stage produces value).
        return 0

    if not FRESH_PATH.is_file():
        print("detector-synthesize: no .research-fresh.json yet (first run or stage-1 found nothing). Done.")
        return 0

    try:
        fresh_payload = json.loads(FRESH_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"detector-synthesize: can't read fresh list: {exc}", file=sys.stderr)
        return 1
    fresh = fresh_payload.get("entries", [])
    if not fresh:
        print("detector-synthesize: fresh list is empty. Nothing to judge.")
        return 0

    repo = os.environ.get("GITHUB_REPOSITORY", "watsonrm/tokenmin-scanner")
    model = os.environ.get("TOKENMIN_SYNTH_MODEL", DEFAULT_MODEL)
    try:
        budget_usd = float(os.environ.get("TOKENMIN_SYNTH_BUDGET", DEFAULT_BUDGET_USD))
    except ValueError:
        budget_usd = DEFAULT_BUDGET_USD
    try:
        max_fetch = int(os.environ.get("TOKENMIN_SYNTH_MAX_FETCH_CHARS", DEFAULT_MAX_FETCH_CHARS))
    except ValueError:
        max_fetch = DEFAULT_MAX_FETCH_CHARS

    prompt = PROMPT_PATH.read_text(encoding="utf-8")

    # ISO week date for the digest; e.g. '2026-W21'-style is brittle, prefer
    # the Monday-of-the-week ISO date so it sorts naturally and reads as a date.
    today = datetime.now(tz=timezone.utc).date()
    monday = today.fromordinal(today.toordinal() - today.weekday())
    week_date = monday.isoformat()

    print(
        f"detector-synthesize: {len(fresh)} URL(s) to judge, repo={repo}, "
        f"model={model}, budget=${budget_usd:.2f}"
    )

    digest_issue = _find_or_create_digest(repo, week_date)
    if digest_issue is None:
        print("  ! could not establish digest issue; rows will print to stdout instead.")

    cumulative_cost = 0.0
    judged = 0
    skipped_cost_cap = 0
    skipped_fetch_fail = 0
    filed = 0

    for entry in fresh:
        url = entry.get("url", "")
        feed = entry.get("source", "?")
        if not url:
            continue
        if cumulative_cost >= budget_usd:
            skipped_cost_cap += 1
            row = f"| {url} | {feed} | not triaged (cost cap) | — |"
            if digest_issue:
                _append_digest_row(repo, digest_issue, row)
            else:
                print("  " + row)
            continue

        # Fetch the page.
        try:
            page = _fetch_url_content(url, max_fetch)
        except (urllib.error.URLError, urllib.error.HTTPError, OSError) as exc:
            print(f"  ! fetch failed for {url}: {exc}", file=sys.stderr)
            skipped_fetch_fail += 1
            row = f"| {url} | {feed} | fetch failed | — |"
            if digest_issue:
                _append_digest_row(repo, digest_issue, row)
            continue

        user_msg = (
            f"Source feed: {feed}\nSource URL: {url}\n\n"
            f"--- BEGIN PAGE CONTENT ---\n{page}\n--- END PAGE CONTENT ---"
        )

        try:
            resp = _call_anthropic(api_key, model, prompt, user_msg)
        except (urllib.error.URLError, urllib.error.HTTPError, OSError) as exc:
            print(f"  ! anthropic call failed for {url}: {exc}", file=sys.stderr)
            row = f"| {url} | {feed} | api error | — |"
            if digest_issue:
                _append_digest_row(repo, digest_issue, row)
            continue

        usage = resp.get("usage", {}) or {}
        call_cost = _usage_cost_usd(model, usage)
        cumulative_cost += call_cost

        try:
            verdict = _verdict_from_response(resp)
        except (ValueError, json.JSONDecodeError) as exc:
            print(f"  ! verdict parse failed for {url}: {exc}", file=sys.stderr)
            row = f"| {url} | {feed} | parse error | — |"
            if digest_issue:
                _append_digest_row(repo, digest_issue, row)
            continue

        judged += 1
        is_candidate = bool(verdict.get("candidate"))
        if is_candidate:
            issue_url = _file_candidate_issue(repo, url, feed, model, call_cost, verdict)
            if issue_url:
                filed += 1
                # Extract the issue number for the digest row.
                m = re.search(r"/issues/(\d+)$", issue_url)
                ref = f"#{m.group(1)}" if m else issue_url
                row = f"| {url} | {feed} | yes | {ref} |"
            else:
                row = f"| {url} | {feed} | yes (file failed) | — |"
        else:
            reason = (verdict.get("reason") or "").replace("|", "\\|")[:120]
            row = f'| {url} | {feed} | no — "{reason}" | — |'

        if digest_issue:
            _append_digest_row(repo, digest_issue, row)
        else:
            print("  " + row)

    print(
        f"  done: judged={judged}, filed={filed}, "
        f"skipped_cost_cap={skipped_cost_cap}, skipped_fetch_fail={skipped_fetch_fail}, "
        f"cost=${cumulative_cost:.4f} / ${budget_usd:.2f}"
    )

    # Clear the fresh list so the next stage-1 run isn't re-fed the same URLs
    # if it produces zero new entries. We don't `git add` it — fresh is a
    # transient artifact (and gitignored).
    FRESH_PATH.write_text(json.dumps({"entries": []}, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
