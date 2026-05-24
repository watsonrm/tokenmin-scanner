#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Tokenmin detector-research watcher.

Runs from GitHub Actions on a weekly schedule. Pure stdlib (urllib + json
+ re) — no pip install in CI. Watches public sources that publish Claude
usage / optimization patterns, diffs against a checked-in state file, and
opens one GitHub issue per newly-seen post tagged for human triage.

Sources watched (each defined as fetch_<name>() below):
  - Anthropic news index
  - Claude Code docs changelog / release notes (when published)
  - anthropics/claude-code GitHub releases
  - anthropics/anthropic-cookbook GitHub commits (recent)

Design choices:
  - Stdlib only so CI doesn't need a venv.
  - State file (bin/.research-seen.json) committed to repo by the workflow.
    Survives laptop shutdown by virtue of living in the repo, not on a
    user machine.
  - Each source returns a list of {url, title, snippet, source} dicts.
    Diff vs seen URLs → file an issue per new entry.
  - Throttled: at most 5 new issues per run (avoid flooding when a source
    publishes a backlog or schema changes).
  - Idempotent: re-running on the same state files no issues.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Iterable


STATE_PATH = Path(__file__).resolve().parent / ".research-seen.json"
MAX_NEW_ISSUES_PER_RUN = 5
USER_AGENT = "tokenmin-detector-research/1.0 (+https://github.com/watsonrm/tokenmin-scanner)"
TIMEOUT_SEC = 20


def _fetch(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/json"})
    with urllib.request.urlopen(req, timeout=TIMEOUT_SEC) as r:
        return r.read().decode("utf-8", errors="replace")


def _safe(fn):
    """Wrap source fetchers so one broken source doesn't kill the whole run."""
    try:
        return fn()
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, ValueError) as exc:
        print(f"  ! {fn.__name__} failed: {exc}", file=sys.stderr)
        return []


# ----- sources --------------------------------------------------------------

def fetch_anthropic_news() -> list[dict]:
    """Scrape anthropic.com/news index for post slugs + titles."""
    html = _fetch("https://www.anthropic.com/news")
    # Anthropic uses /news/<slug> hrefs with the title inside an <h3> nearby.
    out: list[dict] = []
    pattern = re.compile(r'href="(/news/[a-z0-9\-]+)"[^>]*>\s*(?:<[^>]+>\s*)*([^<]{10,200})', re.IGNORECASE)
    seen_slugs = set()
    for m in pattern.finditer(html):
        slug = m.group(1)
        if slug in seen_slugs:
            continue
        seen_slugs.add(slug)
        title = re.sub(r"\s+", " ", m.group(2)).strip()
        out.append({
            "url": f"https://www.anthropic.com{slug}",
            "title": title,
            "snippet": "",
            "source": "anthropic-news",
        })
    return out


def fetch_anthropic_engineering() -> list[dict]:
    """Scrape anthropic.com/engineering index — where the meaty token-usage posts live."""
    html = _fetch("https://www.anthropic.com/engineering")
    out: list[dict] = []
    pattern = re.compile(r'href="(/engineering/[a-z0-9\-]+)"[^>]*>\s*(?:<[^>]+>\s*)*([^<]{10,200})', re.IGNORECASE)
    seen = set()
    for m in pattern.finditer(html):
        slug = m.group(1)
        if slug in seen:
            continue
        seen.add(slug)
        title = re.sub(r"\s+", " ", m.group(2)).strip()
        out.append({
            "url": f"https://www.anthropic.com{slug}",
            "title": title,
            "snippet": "",
            "source": "anthropic-engineering",
        })
    return out


def fetch_claude_code_releases() -> list[dict]:
    """GitHub releases for anthropics/claude-code — new versions often change usage patterns."""
    try:
        body = _fetch("https://api.github.com/repos/anthropics/claude-code/releases?per_page=20")
        releases = json.loads(body)
    except (json.JSONDecodeError, urllib.error.HTTPError):
        return []
    out: list[dict] = []
    for r in releases:
        if not isinstance(r, dict):
            continue
        out.append({
            "url": r.get("html_url", ""),
            "title": f"claude-code {r.get('tag_name', '?')}: {r.get('name', '')}".strip(),
            "snippet": (r.get("body") or "")[:600],
            "source": "claude-code-releases",
        })
    return [r for r in out if r["url"]]


def fetch_cookbook_commits() -> list[dict]:
    """Recent commits on anthropics/anthropic-cookbook — new examples often signal new patterns worth detecting."""
    try:
        body = _fetch("https://api.github.com/repos/anthropics/anthropic-cookbook/commits?per_page=20")
        commits = json.loads(body)
    except (json.JSONDecodeError, urllib.error.HTTPError):
        return []
    out: list[dict] = []
    for c in commits:
        if not isinstance(c, dict):
            continue
        msg = (c.get("commit") or {}).get("message", "")
        first_line = msg.splitlines()[0] if msg else ""
        # Filter noise — only meaningful additions / new examples
        if not any(kw in first_line.lower() for kw in ("add", "new", "example", "guide", "pattern", "optim")):
            continue
        out.append({
            "url": c.get("html_url", ""),
            "title": f"cookbook: {first_line[:160]}",
            "snippet": msg[:600],
            "source": "cookbook",
        })
    return [c for c in out if c["url"]]


SOURCES = [
    fetch_anthropic_news,
    fetch_anthropic_engineering,
    fetch_claude_code_releases,
    fetch_cookbook_commits,
]


# ----- main loop ------------------------------------------------------------

def _load_state() -> dict:
    if STATE_PATH.is_file():
        try:
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass
    return {"seen_urls": []}


def _save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _file_issue(repo: str, entry: dict) -> bool:
    """Open a GitHub issue via gh CLI. Returns True on success."""
    title = f"Research candidate ({entry['source']}): {entry['title'][:180]}"
    body = "\n".join([
        f"**Source:** {entry['source']}",
        f"**URL:** {entry['url']}",
        "",
        "## Snippet",
        "",
        f"> {entry['snippet'] or '(no snippet — open the URL for context)'}",
        "",
        "## Triage",
        "",
        "- [ ] Read the source",
        "- [ ] Is there a usage / optimization pattern here that tokenmin doesn't already detect?",
        "- [ ] If yes: file a follow-up issue with detector design (signal + evidence + pillar + tier per scanner#9 format)",
        "- [ ] If no: close as `not-applicable`",
        "",
        "---",
        "_Auto-filed by `bin/detector-research.py` on its weekly cron. "
        "Close with `not-applicable` if this source doesn't suggest a new detector._",
    ])
    try:
        subprocess.run(
            ["gh", "issue", "create",
             "--repo", repo,
             "--title", title,
             "--body", body,
             "--label", "research-candidate"],
            check=True, capture_output=True, text=True, timeout=30,
        )
        print(f"  + filed: {title[:80]}")
        return True
    except subprocess.CalledProcessError as exc:
        # 'label not found' is non-fatal — retry without it.
        if "not found" in (exc.stderr or "") and "label" in (exc.stderr or "").lower():
            try:
                subprocess.run(
                    ["gh", "issue", "create", "--repo", repo, "--title", title, "--body", body],
                    check=True, capture_output=True, text=True, timeout=30,
                )
                print(f"  + filed (no label): {title[:80]}")
                return True
            except subprocess.CalledProcessError as exc2:
                print(f"  ! gh issue create failed: {exc2.stderr}", file=sys.stderr)
                return False
        print(f"  ! gh issue create failed: {exc.stderr}", file=sys.stderr)
        return False


def main() -> int:
    repo = os.environ.get("GITHUB_REPOSITORY", "watsonrm/tokenmin-scanner")
    print(f"detector-research: scanning {len(SOURCES)} sources, target repo={repo}")
    state = _load_state()
    seen = set(state.get("seen_urls", []))
    print(f"  state: {len(seen)} URL(s) previously seen")

    candidates: list[dict] = []
    for src in SOURCES:
        items = _safe(src)
        print(f"  {src.__name__}: {len(items)} item(s)")
        candidates.extend(items)

    # Dedupe within this run.
    fresh: list[dict] = []
    seen_in_run = set()
    for c in candidates:
        if c["url"] in seen or c["url"] in seen_in_run:
            continue
        seen_in_run.add(c["url"])
        fresh.append(c)

    print(f"  fresh: {len(fresh)} new URL(s)")
    if not fresh:
        print("  nothing new this week. Done.")
        return 0

    # Throttle — file at most N issues per run, mark the rest as seen anyway
    # so they don't pile up next week (rate-limit storms).
    to_file = fresh[:MAX_NEW_ISSUES_PER_RUN]
    overflow = fresh[MAX_NEW_ISSUES_PER_RUN:]
    if overflow:
        print(f"  overflow: {len(overflow)} item(s) marked seen without filing (rate limit)")

    filed = 0
    for entry in to_file:
        if _file_issue(repo, entry):
            seen.add(entry["url"])
            filed += 1
    for entry in overflow:
        seen.add(entry["url"])

    state["seen_urls"] = sorted(seen)
    _save_state(state)
    print(f"  done: {filed} issue(s) filed, state updated ({len(seen)} URL(s) tracked)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
