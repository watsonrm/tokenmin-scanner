#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Tokenmin detector-research watcher.

Runs from GitHub Actions on a weekly schedule. Pure stdlib (urllib + json
+ re + xml.etree) — no pip install in CI. Watches a curated list of
public sources for new Claude usage / optimization patterns, diffs against
a checked-in state file, and opens one GitHub issue per newly-seen post
tagged for human triage.

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

def fetch_html_index(name: str, base_url: str, index_url: str, slug_prefix: str) -> list[dict]:
    """Scrape an index page for hrefs matching slug_prefix + visible title text."""
    html = _fetch(index_url)
    # Allow letters, digits, hyphens, and slashes after the prefix so this works
    # for both flat (/news/<slug>) and nested (/docs/en/<area>/<slug>) URL shapes.
    pattern = re.compile(
        r'href="(' + re.escape(slug_prefix) + r'[a-z0-9\-/]+)"[^>]*>\s*(?:<[^>]+>\s*)*([^<]{10,200})',
        re.IGNORECASE,
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
        title = re.sub(r"\s+", " ", m.group(2)).strip()
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


def main() -> int:
    repo = os.environ.get("GITHUB_REPOSITORY", "watsonrm/tokenmin-scanner")
    sources = _load_sources()
    print(f"detector-research: scanning {len(sources)} sources, target repo={repo}")
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
    if not fresh:
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
    print(f"  done: {filed} issue(s) filed, state updated ({len(seen)} URL(s) tracked)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
