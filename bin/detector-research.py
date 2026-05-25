#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Tokenmin detector-research watcher — Stage 1 (URL discovery).

Runs from GitHub Actions on a weekly schedule. Pure stdlib (urllib + json
+ re + xml.etree) — no pip install in CI. Watches a curated list of
public sources for new Claude usage / optimization patterns, diffs against
a checked-in state file, and hands the fresh-URL list off to Stage 2
(`bin/detector-synthesize.py`) for Claude-judged synthesis.

Two-stage architecture (added 2026-05-24, see scanner#15):
  Stage 1 (this file): URL discovery. Diff vs .research-seen.json,
    write the fresh list to .research-fresh.json, commit updated seen state.
    DOES NOT file per-URL issues — that was the v1 behavior (`--legacy-file-issues`
    preserves it as an escape hatch).
  Stage 2 (detector-synthesize.py): fetch each fresh URL, ask Claude whether
    it suggests a new detector tokenmin doesn't already have, file a
    properly-formatted candidate issue only on real signals.

Source list lives in bin/sources.json — edit there to add/remove. Each
source declares a tier (1=Anthropic first-party, auto-trusted; 2=verified
community, curated allowlist) and a trust_reason. PRs to add a source
should open an issue first so the trust signal can be confirmed.

Source types:
  - html-index: scrape an index page for href slugs (Anthropic news,
    engineering, code/api docs)
  - github-releases: GitHub repo releases API
  - github-commits: GitHub repo commits API (with optional keyword filter)
  - rss: RSS 2.0 or Atom feed

Design choices:
  - Stdlib only so CI doesn't need a venv.
  - State file (bin/.research-seen.json) committed to repo by the workflow.
    Survives laptop shutdown by virtue of living in the repo, not on a
    user machine.
  - Each fetcher returns a list of {url, title, snippet, source} dicts.
    Diff vs seen URLs → file an issue per new entry.
  - Throttled: at most 5 new issues per run (avoid flooding when a source
    publishes a backlog or a new source is first added).
  - Idempotent: re-running on the same state files no issues.
  - Failure-isolated: one broken source doesn't kill the whole run.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path


BIN_DIR = Path(__file__).resolve().parent
STATE_PATH = BIN_DIR / ".research-seen.json"
FRESH_PATH = BIN_DIR / ".research-fresh.json"
SOURCES_PATH = BIN_DIR / "sources.json"
MAX_NEW_ISSUES_PER_RUN = 5
USER_AGENT = "tokenmin-detector-research/2.0 (+https://github.com/watsonrm/tokenmin-scanner)"
TIMEOUT_SEC = 20


def _fetch(url: str) -> str:
    req = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/json,application/rss+xml,application/xml"},
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT_SEC) as r:
        return r.read().decode("utf-8", errors="replace")


def _safe(label: str, fn, *args, **kwargs) -> list[dict]:
    """Wrap source fetchers so one broken source doesn't kill the whole run."""
    try:
        return fn(*args, **kwargs)
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, ValueError, ET.ParseError) as exc:
        print(f"  ! {label} failed: {exc}", file=sys.stderr)
        return []


# ----- fetcher types --------------------------------------------------------

# Strings that are technically valid 10-200 char text but never a real post title.
# Card chrome on anthropic.com/news + most CMS templates renders these alongside
# titles, and the old scraper kept grabbing them instead of the title.
_DATE_LIKE = re.compile(
    r"^(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d{1,2},?\s+\d{4}\s*$",
    re.IGNORECASE,
)
_CATEGORY_LABELS = {
    "announcements", "announcement", "product", "news", "engineering",
    "research", "policy", "company", "security", "interpretability",
    "alignment", "education", "customers", "press", "blog", "podcast",
}


def _looks_like_post_title(text: str) -> bool:
    """Heuristic: does this string look like an actual post title vs card chrome?

    Filters out: dates, single-word category labels, "May 25, 2026"-style
    timestamps, anything <10 chars or >200 chars after normalization.
    """
    if not text:
        return False
    text = text.strip()
    if len(text) < 10 or len(text) > 200:
        return False
    if _DATE_LIKE.match(text):
        return False
    words = text.split()
    # Single-word labels are almost always category chips, not titles.
    if len(words) <= 1:
        return False
    # Two-word strings that are category labels (e.g. "Product announcements") —
    # check if every word lowercased is in the noise set.
    if len(words) <= 2 and all(w.lower().rstrip(".,") in _CATEGORY_LABELS for w in words):
        return False
    return True


def _strip_html_tags(html: str) -> str:
    """Strip all HTML tags + collapse whitespace + decode common entities."""
    no_tags = re.sub(r"<[^>]+>", " ", html)
    no_tags = re.sub(r"\s+", " ", no_tags).strip()
    # Decode the entities that show up in real-world HTML — keep it cheap;
    # Python's html.unescape would be more thorough but it's a stdlib import
    # we can avoid.
    for entity, char in (
        ("&#x27;", "'"), ("&#39;", "'"),
        ("&amp;", "&"),
        ("&quot;", '"'), ("&#34;", '"'),
        ("&lt;", "<"), ("&gt;", ">"),
        ("&nbsp;", " "), ("&mdash;", "—"), ("&ndash;", "–"),
        ("&hellip;", "…"), ("&rsquo;", "'"), ("&lsquo;", "'"),
        ("&rdquo;", "”"), ("&ldquo;", "“"),
    ):
        no_tags = no_tags.replace(entity, char)
    return no_tags


def _extract_post_title(inner_html: str) -> str | None:
    """Find the best post-title candidate inside the inner HTML of a link.

    Strategy (in order):
      1. Look for heading tags inside the link (`<h1>`..`<h4>`). Use the first
         heading whose text looks like a title.
      2. Look for elements with class names containing "title" or "headline".
      3. Fall back to the longest text chunk that passes the title heuristic.
    """
    # Remove script/style blocks defensively.
    cleaned = re.sub(
        r"<(script|style)\b[^>]*>.*?</\1>",
        "",
        inner_html,
        flags=re.IGNORECASE | re.DOTALL,
    )

    # 1. Headings.
    for tag in ("h1", "h2", "h3", "h4"):
        for m in re.finditer(
            rf"<{tag}\b[^>]*>(.*?)</{tag}>", cleaned, re.IGNORECASE | re.DOTALL
        ):
            text = _strip_html_tags(m.group(1))
            if _looks_like_post_title(text):
                return text

    # 2. Title-ish class names.
    for m in re.finditer(
        r'<[^>]*class="[^"]*(?:title|headline)[^"]*"[^>]*>(.*?)</',
        cleaned,
        re.IGNORECASE | re.DOTALL,
    ):
        text = _strip_html_tags(m.group(1))
        if _looks_like_post_title(text):
            return text

    # 3. Longest text chunk that looks like a title.
    chunks = re.findall(r">([^<]{10,200})<", cleaned)
    titles = [
        re.sub(r"\s+", " ", c).strip()
        for c in chunks
    ]
    titles = [t for t in titles if _looks_like_post_title(t)]
    if not titles:
        return None
    return max(titles, key=len)


def fetch_html_index(name: str, base_url: str, index_url: str, slug_prefix: str) -> list[dict]:
    """Scrape an index page for hrefs matching slug_prefix + extract their post titles.

    Pattern: capture the FULL `<a>...</a>` block, then extract the title from
    its inner HTML using heading tags / title-class elements / longest-chunk
    fallback. The prior heuristic ("first text after href") grabbed card chrome
    like dates and category labels — anthropic.com/news routinely renders
    "<a><h3>Real Title</h3><span>May 25, 2026</span><span>Announcements</span></a>"
    and the old regex captured "May 25, 2026" as the title.
    """
    html = _fetch(index_url)
    pattern = re.compile(
        r'<a\b[^>]*href="('
        + re.escape(slug_prefix)
        + r'[a-z0-9\-/]+)"[^>]*>(.*?)</a>',
        re.IGNORECASE | re.DOTALL,
    )
    seen_slugs: set[str] = set()
    out: list[dict] = []
    for m in pattern.finditer(html):
        slug = m.group(1)
        # Skip the bare-prefix index URL itself (e.g. /news/, /docs/en/).
        if slug.rstrip("/") + "/" == slug_prefix:
            continue
        if slug in seen_slugs:
            continue
        seen_slugs.add(slug)
        title = _extract_post_title(m.group(2))
        if not title:
            # Better to skip than to file noise. The URL stays unfiled this
            # run and may be picked up next time if the page restructures.
            continue
        out.append({
            "url": f"{base_url}{slug}",
            "title": title,
            "snippet": "",
            "source": name,
        })
    return out


def fetch_github_releases(name: str, repo: str) -> list[dict]:
    body = _fetch(f"https://api.github.com/repos/{repo}/releases?per_page=20")
    try:
        releases = json.loads(body)
    except json.JSONDecodeError:
        return []
    out: list[dict] = []
    for r in releases:
        if not isinstance(r, dict):
            continue
        url = r.get("html_url", "")
        if not url:
            continue
        out.append({
            "url": url,
            "title": f"{repo} {r.get('tag_name', '?')}: {r.get('name', '')}".strip(),
            "snippet": (r.get("body") or "")[:600],
            "source": name,
        })
    return out


def fetch_github_commits(name: str, repo: str, keyword_filter: list[str] | None = None) -> list[dict]:
    body = _fetch(f"https://api.github.com/repos/{repo}/commits?per_page=20")
    try:
        commits = json.loads(body)
    except json.JSONDecodeError:
        return []
    keywords = [k.lower() for k in (keyword_filter or [])]
    out: list[dict] = []
    for c in commits:
        if not isinstance(c, dict):
            continue
        msg = (c.get("commit") or {}).get("message", "")
        first_line = msg.splitlines()[0] if msg else ""
        if keywords and not any(kw in first_line.lower() for kw in keywords):
            continue
        url = c.get("html_url", "")
        if not url:
            continue
        out.append({
            "url": url,
            "title": f"{repo}: {first_line[:160]}",
            "snippet": msg[:600],
            "source": name,
        })
    return out


def _strip_html(s: str) -> str:
    """Cheap HTML-tag strip + entity decode for RSS snippets. Stdlib-only."""
    s = re.sub(r"<[^>]+>", " ", s)
    s = (s.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
           .replace("&quot;", '"').replace("&#39;", "'").replace("&nbsp;", " "))
    return re.sub(r"\s+", " ", s).strip()


def fetch_rss(name: str, feed_url: str, keyword_filter: list[str] | None = None) -> list[dict]:
    """Parse RSS 2.0 or Atom. Returns entries with title + canonical link + snippet.

    If `keyword_filter` is set, only return entries whose title or snippet
    (case-insensitive) contains at least one of the keywords. Use this for
    high-volume Tier-2 feeds where the publisher covers many topics — keeps
    the watcher from filing issues about unrelated posts.
    """
    body = _fetch(feed_url)
    root = ET.fromstring(body)
    keywords = [k.lower() for k in (keyword_filter or [])]

    def _matches(title: str, snippet: str) -> bool:
        if not keywords:
            return True
        hay = (title + " " + snippet).lower()
        return any(kw in hay for kw in keywords)

    out: list[dict] = []
    # RSS 2.0: <rss><channel><item>...
    for item in root.iter("item"):
        title_el = item.find("title")
        link_el = item.find("link")
        desc_el = item.find("description")
        url = (link_el.text or "").strip() if link_el is not None else ""
        title = (title_el.text or "").strip() if title_el is not None else ""
        snippet = _strip_html(desc_el.text or "") if desc_el is not None else ""
        if url and _matches(title, snippet):
            out.append({"url": url, "title": title, "snippet": snippet[:600], "source": name})
    # Atom: <feed><entry>... — use the namespaced tag.
    atom_ns = "{http://www.w3.org/2005/Atom}"
    for entry in root.iter(f"{atom_ns}entry"):
        title_el = entry.find(f"{atom_ns}title")
        link_el = entry.find(f"{atom_ns}link")
        summary_el = entry.find(f"{atom_ns}summary") or entry.find(f"{atom_ns}content")
        url = link_el.get("href", "") if link_el is not None else ""
        title = (title_el.text or "").strip() if title_el is not None else ""
        snippet = _strip_html(summary_el.text or "") if summary_el is not None else ""
        if url and _matches(title, snippet):
            out.append({"url": url, "title": title, "snippet": snippet[:600], "source": name})
    return out


FETCHERS = {
    "html-index": lambda s: fetch_html_index(s["name"], s["base_url"], s["index_url"], s["slug_prefix"]),
    "github-releases": lambda s: fetch_github_releases(s["name"], s["repo"]),
    "github-commits": lambda s: fetch_github_commits(s["name"], s["repo"], s.get("keyword_filter")),
    "rss": lambda s: fetch_rss(s["name"], s["feed_url"], s.get("keyword_filter")),
}


# ----- main loop ------------------------------------------------------------

def _load_sources() -> list[dict]:
    raw = json.loads(SOURCES_PATH.read_text(encoding="utf-8"))
    return raw.get("sources", [])


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


def _save_fresh(entries: list[dict]) -> None:
    """Stage-1 → Stage-2 handoff. Stage 2 (detector-synthesize.py) reads this."""
    FRESH_PATH.write_text(
        json.dumps({"entries": entries}, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="tokenmin detector-research stage-1 URL discovery")
    parser.add_argument(
        "--legacy-file-issues",
        action="store_true",
        help=(
            "Escape hatch: restore the v1 behavior and file one GitHub issue per "
            "fresh URL (no Claude judgment). Use this if the synthesis stage is "
            "broken or you want to run stage 1 standalone."
        ),
    )
    args = parser.parse_args(argv)

    repo = os.environ.get("GITHUB_REPOSITORY", "watsonrm/tokenmin-scanner")
    sources = _load_sources()
    mode = "legacy (per-URL issues)" if args.legacy_file_issues else "stage-1 (handoff to synthesis)"
    print(f"detector-research [{mode}]: scanning {len(sources)} sources, target repo={repo}")
    state = _load_state()
    seen = set(state.get("seen_urls", []))
    print(f"  state: {len(seen)} URL(s) previously seen")

    candidates: list[dict] = []
    for src in sources:
        fetcher = FETCHERS.get(src["type"])
        if fetcher is None:
            print(f"  ! {src['name']}: unknown type {src['type']!r}, skipping", file=sys.stderr)
            continue
        items = _safe(src["name"], fetcher, src)
        tier = src.get("tier", "?")
        print(f"  {src['name']} (tier {tier}): {len(items)} item(s)")
        candidates.extend(items)

    # Dedupe within this run.
    fresh: list[dict] = []
    seen_in_run: set[str] = set()
    for c in candidates:
        if c["url"] in seen or c["url"] in seen_in_run:
            continue
        seen_in_run.add(c["url"])
        fresh.append(c)

    print(f"  fresh: {len(fresh)} new URL(s)")

    if not args.legacy_file_issues:
        # Stage-1 default: hand the full fresh list to stage 2. The synthesis
        # stage applies its own budget (cost cap), so we don't throttle here.
        # Mark all fresh URLs as seen now — stage 2 is best-effort and we
        # don't want to re-judge the same URL on the next cron if stage 2
        # ran out of budget halfway through. The digest issue records what
        # got skipped this week.
        _save_fresh(fresh)
        for entry in fresh:
            seen.add(entry["url"])
        state["seen_urls"] = sorted(seen)
        _save_state(state)
        print(
            f"  done (stage 1): wrote {len(fresh)} URL(s) to {FRESH_PATH.name}, "
            f"state updated ({len(seen)} URL(s) tracked). Stage 2 will judge."
        )
        return 0

    # Legacy path: file one issue per URL, no synthesis. Preserves v1 behavior
    # for manual runs / synthesis-broken fallback.
    if not fresh:
        # Still touch the fresh artifact so stage 2 (if it runs anyway) is a no-op.
        _save_fresh([])
        print("  nothing new this week. Done.")
        return 0

    # Throttle — file at most N issues per run, mark the rest as seen anyway
    # so they don't pile up next week (rate-limit storms / first-run baselines).
    to_file = fresh[:MAX_NEW_ISSUES_PER_RUN]
    overflow = fresh[MAX_NEW_ISSUES_PER_RUN:]
    if overflow:
        print(f"  overflow: {len(overflow)} item(s) marked seen without filing (rate limit / first-run baseline)")

    filed = 0
    for entry in to_file:
        if _file_issue(repo, entry):
            seen.add(entry["url"])
            filed += 1
    for entry in overflow:
        seen.add(entry["url"])

    state["seen_urls"] = sorted(seen)
    _save_state(state)
    # Legacy mode bypasses stage 2 — empty the fresh artifact so a later
    # stage-2 run on the same checkout doesn't double-process these URLs.
    _save_fresh([])
    print(f"  done (legacy): {filed} issue(s) filed, state updated ({len(seen)} URL(s) tracked)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
