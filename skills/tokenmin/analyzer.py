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
# Rough USD per million tokens. Update as needed.
PRICING: dict[str, tuple[float, float, float, float]] = {
    # model_substring: (input, output, cache_write, cache_read)
    "opus":   (15.00, 75.00, 18.75, 1.50),
    "sonnet": ( 3.00, 15.00,  3.75, 0.30),
    "haiku":  ( 0.80,  4.00,  1.00, 0.08),
}


def _price_for(model: str | None) -> tuple[float, float, float, float]:
    if not model:
        return PRICING["sonnet"]
    m = model.lower()
    for key, prices in PRICING.items():
        if key in m:
            return prices
    return PRICING["sonnet"]


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


def _safe_jsonl(path: Path):
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
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
        for proj in projects_dir.iterdir():
            if not proj.is_dir():
                continue
            snap.projects_total += 1
            # Claude Code mangles project paths into dir names; the real project
            # path is encoded in the dir name. We can't reliably stat the source
            # repo, but we can look for sidecar files dropped in the project dir.
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
