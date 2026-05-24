# SPDX-License-Identifier: Apache-2.0
"""Walk ~/.claude and build a structured usage snapshot.

Defensive about JSONL schema — Claude Code's session format has evolved.
Anything we can't parse is counted and skipped, not crashed on.
"""
from __future__ import annotations

import json
import os
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


# --- pricing ----------------------------------------------------------------
# Pricing tables now live in engine/pricing.json so a single edit + auto-update
# refreshes every install when Anthropic changes rates. See engine/pricing.py
# for the loader + staleness check.
import sys as _sys
from pathlib import Path as _Path
_engine_dir = _Path(__file__).resolve().parent.parent.parent / "engine"
if str(_engine_dir) not in _sys.path:
    _sys.path.insert(0, str(_engine_dir))
try:
    from pricing import price_for as _price_for  # type: ignore
except ImportError:
    # Engine not bundled (scanner-only install) — fall back to static rates.
    # These match the bundled pricing.json defaults; if they ever drift, the
    # scanner-only path silently uses these instead.
    _STATIC_PRICING = {
        "opus":   (15.00, 75.00, 18.75, 1.50),
        "sonnet": ( 3.00, 15.00,  3.75, 0.30),
        "haiku":  ( 0.80,  4.00,  1.00, 0.08),
    }
    def _price_for(model):
        if not model:
            return _STATIC_PRICING["sonnet"]
        ml = model.lower()
        for key, prices in _STATIC_PRICING.items():
            if key in ml:
                return prices
        return _STATIC_PRICING["sonnet"]


# --- data classes -----------------------------------------------------------

@dataclass
class SessionStats:
    session_id: str
    project: str
    started_at: float | None = None
    ended_at: float | None = None
    user_turns: int = 0
    assistant_turns: int = 0
    tool_calls: Counter = field(default_factory=Counter)
    tools_per_turn: list[int] = field(default_factory=list)
    files_read: Counter = field(default_factory=Counter)
    files_written: set[str] = field(default_factory=set)
    permission_denies: int = 0
    error_results: int = 0
    long_searches: int = 0  # grep/find/glob bursts
    agents_used: Counter = field(default_factory=Counter)
    models_used: Counter = field(default_factory=Counter)
    input_tokens: int = 0
    output_tokens: int = 0
    cache_write_tokens: int = 0
    cache_read_tokens: int = 0
    est_cost_usd: float = 0.0
    redo_signals: int = 0  # "actually", "no wait", "instead", "undo"


@dataclass
class ConfigSnapshot:
    has_global_settings: bool = False
    has_global_claude_md: bool = False
    global_claude_md_lines: int = 0
    obsolete_references: list[str] = field(default_factory=list)
    global_hook_count: int = 0
    permission_count: int = 0
    custom_agents: list[str] = field(default_factory=list)
    custom_skills: list[str] = field(default_factory=list)
    custom_commands: list[str] = field(default_factory=list)
    mcp_servers: list[str] = field(default_factory=list)
    projects_with_claude_md: int = 0
    projects_with_oversized_claude_md: int = 0
    projects_total: int = 0
    projects_with_local_settings: int = 0
    projects_with_local_agents: int = 0
    # v0.12.4: walk each project's session JSONL `cwd` field to find the real
    # source-repo path, then probe THAT path for project-level CLAUDE.md and
    # .claude/agents/. Without this, the engine fires no_global_claude_md /
    # no_custom_agents as 95%-confidence findings on users whose entire setup
    # lives at the project level — scanner issue #7.
    project_cwd_total: int = 0
    project_cwd_with_claude_md: int = 0
    project_cwd_with_local_agents: int = 0
    project_cwd_with_agent_count: int = 0  # sum of agents across those .claude/agents/ dirs
    # Output-style configuration. v0.5: surface absence of `outputStyle` so the
    # engine can recommend the one-line config change Anthropic measures at
    # 40–65% output-token reduction.
    output_style: str | None = None
    # Tool-search runtime env. v0.5: when many MCP servers are connected,
    # ENABLE_TOOL_SEARCH=auto recovers ~70K context tokens (Anthropic measured
    # 191,300 → 122,800). Surface its presence so the detector can recommend.
    enable_tool_search: str | None = None
    # Billing plan for Claude (e.g. api, pro, max, unknown)
    billing_plan: str | None = None



@dataclass
class Snapshot:
    """Everything Tokenmin needs to write a report."""
    generated_at: float
    window_days: int
    sessions: list[SessionStats]
    config: ConfigSnapshot
    parse_errors: int = 0
    skipped_files: int = 0

    @property
    def total_cost(self) -> float:
        return sum(s.est_cost_usd for s in self.sessions)

    @property
    def total_input_tokens(self) -> int:
        return sum(s.input_tokens for s in self.sessions)

    @property
    def total_output_tokens(self) -> int:
        return sum(s.output_tokens for s in self.sessions)

    @property
    def tool_mix(self) -> Counter:
        c: Counter = Counter()
        for s in self.sessions:
            c.update(s.tool_calls)
        return c


# --- session parsing --------------------------------------------------------

_REDO_HINTS = (
    "actually", "no wait", "instead", "undo", "revert", "scratch that",
    "never mind", "wrong", "go back",
)

_LONG_SEARCH_TOOLS = {"Grep", "Glob", "find", "Bash"}


_MAX_JSONL_FILE = 50 * 1024 * 1024   # skip files > 50 MiB outright
_MAX_JSONL_LINE = 1024 * 1024        # cap per-line read at 1 MiB


def _safe_jsonl(path: Path):
    """Defense against adversarial JSONL: bounded per-line + per-file reads.
    Without these caps, a single multi-GB line in a planted session file
    would OOM Python during the iter-lines read."""
    try:
        if path.stat().st_size > _MAX_JSONL_FILE:
            return
        with path.open("r", encoding="utf-8", errors="replace") as f:
            while True:
                line = f.readline(_MAX_JSONL_LINE)
                if not line:
                    return
                if len(line) == _MAX_JSONL_LINE and not line.endswith("\n"):
                    # Oversized line — discard up to the next newline (bounded).
                    while True:
                        chunk = f.readline(_MAX_JSONL_LINE)
                        if not chunk or chunk.endswith("\n"):
                            break
                    continue
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue
    except OSError:
        return


def _extract_usage(msg: dict) -> tuple[int, int, int, int, str | None]:
    """Return (in, out, cache_w, cache_r, model)."""
    u = msg.get("usage") or msg.get("message", {}).get("usage") or {}
    model = msg.get("model") or msg.get("message", {}).get("model")
    return (
        int(u.get("input_tokens", 0) or 0),
        int(u.get("output_tokens", 0) or 0),
        int(u.get("cache_creation_input_tokens", 0) or 0),
        int(u.get("cache_read_input_tokens", 0) or 0),
        model,
    )


def _content_blocks(msg: dict) -> list[dict]:
    """Pull content blocks out of however Claude Code happens to wrap them."""
    if "content" in msg and isinstance(msg["content"], list):
        return msg["content"]
    inner = msg.get("message")
    if isinstance(inner, dict):
        c = inner.get("content")
        if isinstance(c, list):
            return c
        if isinstance(c, str):
            return [{"type": "text", "text": c}]
    return []


def _user_text(msg: dict) -> str:
    parts: list[str] = []
    for b in _content_blocks(msg):
        if isinstance(b, dict) and b.get("type") == "text":
            parts.append(b.get("text", ""))
    return " ".join(parts).lower()


def parse_session(jsonl_path: Path, project_name: str, cutoff: float | None) -> SessionStats | None:
    stats = SessionStats(session_id=jsonl_path.stem, project=project_name)
    tools_in_current_turn = 0
    consecutive_searches = 0
    any_events = False
    mtime = jsonl_path.stat().st_mtime if jsonl_path.exists() else 0
    if cutoff is not None and mtime < cutoff:
        return None

    for event in _safe_jsonl(jsonl_path):
        any_events = True
        etype = event.get("type") or event.get("role") or ""
        ts = event.get("timestamp")
        if isinstance(ts, str):
            try:
                t = datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
                if stats.started_at is None:
                    stats.started_at = t
                stats.ended_at = t
            except ValueError:
                pass

        if etype == "user" or event.get("message", {}).get("role") == "user":
            stats.user_turns += 1
            if tools_in_current_turn:
                stats.tools_per_turn.append(tools_in_current_turn)
                tools_in_current_turn = 0
            text = _user_text(event)
            if any(h in text for h in _REDO_HINTS):
                stats.redo_signals += 1

        if etype == "assistant" or event.get("message", {}).get("role") == "assistant":
            stats.assistant_turns += 1
            it, ot, cw, cr, model = _extract_usage(event)
            stats.input_tokens += it
            stats.output_tokens += ot
            stats.cache_write_tokens += cw
            stats.cache_read_tokens += cr
            if model:
                stats.models_used[model] += 1
                pi, po, pcw, pcr = _price_for(model)
                stats.est_cost_usd += (
                    it * pi + ot * po + cw * pcw + cr * pcr
                ) / 1_000_000

            # walk tool uses
            for b in _content_blocks(event):
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "tool_use":
                    tool = b.get("name", "unknown")
                    stats.tool_calls[tool] += 1
                    tools_in_current_turn += 1
                    if tool in _LONG_SEARCH_TOOLS:
                        consecutive_searches += 1
                        if consecutive_searches >= 4:
                            stats.long_searches += 1
                    else:
                        consecutive_searches = 0
                    inp = b.get("input") or {}
                    if tool == "Read":
                        fp = inp.get("file_path")
                        if fp:
                            stats.files_read[fp] += 1
                    elif tool in {"Write", "Edit"}:
                        fp = inp.get("file_path")
                        if fp:
                            stats.files_written.add(fp)
                    elif tool == "Agent":
                        sub = inp.get("subagent_type", "general-purpose")
                        stats.agents_used[sub] += 1

        # tool_result errors / permission denies
        if etype == "tool_result" or "tool_use_id" in event:
            content = event.get("content") or ""
            if isinstance(content, list):
                content = " ".join(
                    b.get("text", "") for b in content if isinstance(b, dict)
                )
            if "permission" in content.lower() and "deni" in content.lower():
                stats.permission_denies += 1
            if event.get("is_error"):
                stats.error_results += 1

    if not any_events:
        return None
    if tools_in_current_turn:
        stats.tools_per_turn.append(tools_in_current_turn)
    return stats


# --- config snapshot --------------------------------------------------------

def _safe_json(path: Path) -> dict | list | None:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def _first_cwd_in_project(proj_dir: Path) -> str | None:
    """Read the first JSONL session in a project dir and return its `cwd` field.

    Claude Code mangles source-repo paths into project dir names by replacing
    `/` and ` ` with `-`, which is lossy (you can't reliably reverse it). But
    every session JSONL records the real CWD in its first message — using
    that is authoritative.

    Returns None if no JSONL is readable. Tries up to 3 files to survive
    occasional corrupted-first-line cases.
    """
    try:
        jsonls = sorted(p for p in proj_dir.iterdir()
                        if p.is_file() and p.suffix == ".jsonl")
    except OSError:
        return None
    for jsonl in jsonls[:3]:
        try:
            with jsonl.open("r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        ev = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    cwd = ev.get("cwd")
                    if isinstance(cwd, str) and cwd:
                        return cwd
        except OSError:
            continue
    return None


def scan_config(claude_home: Path) -> ConfigSnapshot:
    snap = ConfigSnapshot()
    settings = claude_home / "settings.json"
    if settings.exists():
        snap.has_global_settings = True
        data = _safe_json(settings) or {}
        if isinstance(data, dict):
            hooks = data.get("hooks") or {}
            if isinstance(hooks, dict):
                snap.global_hook_count = sum(
                    len(v) if isinstance(v, list) else 1 for v in hooks.values()
                )
            perms = data.get("permissions") or {}
            if isinstance(perms, dict):
                for k in ("allow", "deny", "ask"):
                    v = perms.get(k) or []
                    if isinstance(v, list):
                        snap.permission_count += len(v)
            # v0.5: output-style + tool-search runtime env, both surfaced for
            # the detection layer.
            out_style = data.get("outputStyle")
            if isinstance(out_style, str) and out_style.strip():
                snap.output_style = out_style.strip()
            env = data.get("env") or {}
            if isinstance(env, dict):
                v = env.get("ENABLE_TOOL_SEARCH")
                if isinstance(v, str) and v.strip():
                    snap.enable_tool_search = v.strip()

    claude_md = claude_home / "CLAUDE.md"
    if claude_md.exists():
        snap.has_global_claude_md = True
        try:
            text = claude_md.read_text(encoding="utf-8", errors="replace")
            snap.global_claude_md_lines = sum(1 for _ in text.splitlines())
            # Optimizer flags these as cited-but-non-existent features.
            for ref in (".claudeignore", "/effort 85", "claude --bare"):
                if ref in text:
                    snap.obsolete_references.append(ref)
        except OSError:
            pass

    agents_dir = claude_home / "agents"
    if agents_dir.is_dir():
        snap.custom_agents = sorted(p.stem for p in agents_dir.glob("*.md"))

    skills_dir = claude_home / "skills"
    if skills_dir.is_dir():
        snap.custom_skills = sorted(
            p.parent.name for p in skills_dir.glob("*/SKILL.md")
        )

    commands_dir = claude_home / "commands"
    if commands_dir.is_dir():
        snap.custom_commands = sorted(p.stem for p in commands_dir.glob("*.md"))

    # MCP config — try a few known locations
    for cand in [
        claude_home / "mcp.json",
        claude_home / "claude_desktop_config.json",
        Path.home() / "Library/Application Support/Claude/claude_desktop_config.json",
    ]:
        if cand.exists():
            data = _safe_json(cand) or {}
            servers = data.get("mcpServers") if isinstance(data, dict) else None
            if isinstance(servers, dict):
                snap.mcp_servers = sorted(servers.keys())
                break

    # projects
    projects_dir = claude_home / "projects"
    if projects_dir.is_dir():
        # Track seen CWDs so a project with many session JSONLs only counts
        # once. cwd comes from the first message in each session.
        seen_cwds: set[str] = set()
        for proj in projects_dir.iterdir():
            if not proj.is_dir():
                continue
            snap.projects_total += 1
            # Sidecar files dropped in ~/.claude/projects/<encoded>/ — rare but
            # supported (kept for back-compat with the v0.12.3 schema).
            proj_md = proj / "CLAUDE.md"
            if proj_md.exists():
                snap.projects_with_claude_md += 1
                try:
                    if sum(1 for _ in proj_md.read_text(encoding="utf-8", errors="replace").splitlines()) > 200:
                        snap.projects_with_oversized_claude_md += 1
                except OSError:
                    pass
            if (proj / "settings.json").exists():
                snap.projects_with_local_settings += 1
            if (proj / "agents").is_dir():
                snap.projects_with_local_agents += 1

            # v0.12.4: dig real CWDs from session JSONLs and check the source
            # repos directly. This is the load-bearing change for issue #7.
            cwd = _first_cwd_in_project(proj)
            if cwd is None or cwd in seen_cwds:
                continue
            seen_cwds.add(cwd)
            cwd_path = Path(cwd)
            if not cwd_path.is_dir():
                continue
            snap.project_cwd_total += 1
            if (cwd_path / "CLAUDE.md").is_file():
                snap.project_cwd_with_claude_md += 1
            agents_dir = cwd_path / ".claude" / "agents"
            if agents_dir.is_dir():
                snap.project_cwd_with_local_agents += 1
                try:
                    snap.project_cwd_with_agent_count += sum(
                        1 for p in agents_dir.iterdir() if p.is_file() and p.suffix == ".md"
                    )
                except OSError:
                    pass

    return snap


# --- entry point ------------------------------------------------------------

def collect(claude_home: Path, days: int = 30) -> Snapshot:
    cutoff = time.time() - days * 86400 if days > 0 else None
    sessions: list[SessionStats] = []
    parse_errors = 0
    skipped = 0

    projects_dir = claude_home / "projects"
    if projects_dir.is_dir():
        for proj_dir in projects_dir.iterdir():
            if not proj_dir.is_dir():
                continue
            project_name = proj_dir.name
            for jsonl in proj_dir.glob("*.jsonl"):
                try:
                    s = parse_session(jsonl, project_name, cutoff)
                    if s is None:
                        skipped += 1
                    else:
                        sessions.append(s)
                except Exception:
                    parse_errors += 1

    config = scan_config(claude_home)
    return Snapshot(
        generated_at=time.time(),
        window_days=days,
        sessions=sessions,
        config=config,
        parse_errors=parse_errors,
        skipped_files=skipped,
    )
