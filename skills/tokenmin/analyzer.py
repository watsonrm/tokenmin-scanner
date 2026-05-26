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
    # v0.12.6 — new aggregate fields for the second-wave detectors.
    # Each one is a per-session counter populated in the JSONL walk;
    # detectors aggregate across sessions.
    bash_file_ops: int = 0  # Bash commands matching cat/ls/head/tail/sed/awk/grep/find against a path
    cache_thrash_events: int = 0  # turns where prior gap was 5-55 min AND this turn paid full cache-creation cost
    thinking_bloat_turns: int = 0  # turns with >8K output but <=1 Edit/Write AND no Agent AND short visible message
    hook_event_chars: int = 0  # cumulative chars of detected hook output (heuristic — see analyzer comment)
    hook_event_fires: int = 0
    denied_patterns: Counter = field(default_factory=Counter)  # normalized tool/Bash pattern -> deny count
    compacts: int = 0  # /compact invocations detected in user messages
    compact_then_died: int = 0  # /compact followed by session end within <=3 assistant turns
    opus_compactions: int = 0  # /compact turns where active model was Opus AND input was large (>50K)
    # v0.12.9 — rate-limit errors are the strongest plan-detection signal we have.
    # API users rarely hit them; Pro users hit them constantly; Max users hit
    # them when bursting near quota. Counted separately from generic error_results
    # so the billing-plan heuristic can use them as a signal.
    rate_limit_errors: int = 0
    last_assistant_at: float | None = None  # internal — for gap math; not used outside analyzer
    turns_since_last_compact: int = -1  # internal — -1 = no compact yet this session


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
    # Desktop-only MCP servers — configured in Claude Desktop's config but NOT
    # loaded by Claude Code. Stored separately so detectors that judge
    # "configured but unused" against Code session evidence don't false-positive
    # on a server Code never had access to. See DETECTOR_RULES.md (rule:
    # same-surface comparison).
    mcp_servers_desktop_only: list[str] = field(default_factory=list)
    # Per-server invocation count derived from session tool_calls. Populated
    # by _label_scrub_pass BEFORE tool names get hashed (the hash of
    # `mcp__SERVER__call` and `SERVER` are unrelated, so prefix matching on
    # anonymized names fails — detectors must consult this map instead).
    # Keys match the (post-scrub) entries in `mcp_servers`.
    mcp_server_invocations: dict[str, int] = field(default_factory=dict)
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

# v0.12.6 — Bash commands that should have been dedicated tools (Read/Glob/Grep).
# Matches the START of the command (after optional whitespace). Catches things like
#   cat /etc/hosts
#   ls -la src/
#   head -n 100 logs/foo.log
#   grep -r 'foo' src/
#   sed -n '1,50p' file.txt
# Doesn't catch piped uses (`some_other_cmd | grep ...`) — those are legitimate
# bash pipelines, not file-read substitutes.
import re as _re
_BASH_FILE_OP_PAT = _re.compile(
    r"^(cat|ls|head|tail|sed|awk|grep|find)(\s+|$)",
    _re.IGNORECASE,
)


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
            # v0.12.6: detect /compact invocations (user issues the command).
            # Detection uses the user-visible text — Claude Code transcripts
            # echo the slash command as the user message body.
            stripped = text.strip()
            if stripped.startswith("/compact") and (len(stripped) == 8 or stripped[8] in " \n\t"):
                stats.compacts += 1
                stats.turns_since_last_compact = 0
            # v0.12.6 (heuristic): detect inline hook output.
            # Claude Code wraps hook output in user-message content blocks with
            # tags like <local-command-stdout> or <command-...-output>. We count
            # the cumulative char volume so the hook_token_burner detector has
            # a signal. Marked as HEURISTIC in the detector's confidence (0.4).
            for marker in ("<local-command-stdout>", "<local-command-stderr>",
                           "<command-stdout>", "<command-stderr>"):
                idx = text.find(marker)
                if idx >= 0:
                    end_tag = marker.replace("<", "</")
                    end_idx = text.find(end_tag, idx)
                    if end_idx > idx:
                        stats.hook_event_chars += (end_idx - idx - len(marker))
                        stats.hook_event_fires += 1

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

            # v0.12.6 detector signals on this assistant turn:

            # cache_thrash_short_gaps — turn resumed past TTL but within 1h: paying
            # write cost when a 1h TTL hint or /clear discipline would have made
            # this a read. Gap must be 5-55 min AND cache_creation must dominate.
            if stats.last_assistant_at is not None and t is not None:
                gap = t - stats.last_assistant_at
                if 300 < gap < 3600 and cw > 0 and cr < cw // 4:
                    stats.cache_thrash_events += 1
            stats.last_assistant_at = t

            # opus_for_compaction — the turn immediately after /compact was run by
            # the user. If that turn used Opus AND input was >50K tokens AND it
            # consumed substantial cache_creation, this is the compaction summary
            # on a premium model.
            if stats.turns_since_last_compact == 0:
                model_l = (model or "").lower()
                if "opus" in model_l and (it + cw) > 50_000:
                    stats.opus_compactions += 1
            if stats.turns_since_last_compact >= 0:
                stats.turns_since_last_compact += 1

            # thinking_bloat — measure visible output text + non-tool action
            # surface against output_tokens. If output_tokens is large but visible
            # content + Edit/Write/Agent calls are small, thinking-token budget
            # was burned on a turn that didn't justify it.
            visible_chars = 0
            edit_write = 0
            agent_calls = 0

            # walk tool uses
            for b in _content_blocks(event):
                if not isinstance(b, dict):
                    continue
                btype = b.get("type")
                if btype == "text":
                    visible_chars += len(b.get("text", ""))
                if btype == "tool_use":
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
                        edit_write += 1
                    elif tool == "Agent":
                        sub = inp.get("subagent_type", "general-purpose")
                        stats.agents_used[sub] += 1
                        agent_calls += 1
                    elif tool == "Bash":
                        # bash_cat_instead_of_read — Claude shells out via Bash
                        # for file reads instead of using Read/Glob/Grep.
                        cmd = (inp.get("command") or "").lstrip()
                        if _BASH_FILE_OP_PAT.match(cmd):
                            stats.bash_file_ops += 1

            if ot > 8000 and edit_write <= 1 and agent_calls == 0 and visible_chars < 500:
                stats.thinking_bloat_turns += 1

        # tool_result errors / permission denies
        if etype == "tool_result" or "tool_use_id" in event:
            content = event.get("content") or ""
            if isinstance(content, list):
                content = " ".join(
                    b.get("text", "") for b in content if isinstance(b, dict)
                )
            content_l = content.lower() if isinstance(content, str) else ""
            # v0.12.9: rate-limit detection. Anthropic's rate-limit errors
            # surface in tool_result content with phrases like
            # "rate_limit_error", "rate limit exceeded", or "429". Counted
            # separately from generic error_results so the billing-plan
            # heuristic can use them as a signal (API users rarely hit them;
            # Pro users hit them constantly; Max users only when bursting).
            if (
                "rate_limit" in content_l
                or "rate limit" in content_l
                or "429" in content_l
                and ("limit" in content_l or "throttl" in content_l)
            ):
                stats.rate_limit_errors += 1
            if "permission" in content_l and "deni" in content_l:
                stats.permission_denies += 1
                # v0.12.6 — capture the normalized pattern of what got denied so
                # the permission_denies_loop detector can suggest a real `permissions.deny`
                # entry rather than just "you got denied N times."
                # Heuristic: tool_result content for a denial often references the
                # tool name (e.g. "Permission denied for Bash: rm -rf /tmp/X").
                # Normalize Bash patterns to "Bash: <first-token> ..." so similar
                # invocations cluster.
                snippet = content[:200] if isinstance(content, str) else ""
                pat = _normalize_denied_pattern(snippet)
                if pat:
                    stats.denied_patterns[pat] += 1
            if event.get("is_error"):
                stats.error_results += 1

    if not any_events:
        return None
    if tools_in_current_turn:
        stats.tools_per_turn.append(tools_in_current_turn)
    # v0.12.6 — compact_then_die: if the session ended within <=3 assistant turns
    # of a /compact invocation, the compact was wasted (user should have /clear-ed).
    if 0 < stats.turns_since_last_compact <= 3:
        stats.compact_then_died += 1
    return stats


def _normalize_denied_pattern(snippet: str) -> str:
    """Reduce a deny tool_result snippet to a clusterable key.

    Examples:
      "Permission denied for Bash: rm -rf /tmp/foo" -> "Bash: rm"
      "Permission denied for Write: /etc/passwd"    -> "Write"
    Returns "" if no recognizable tool name found.
    """
    m = _re.search(r"(?:for|in|on)?\s*(Bash|Write|Edit|Read|Grep|Glob|Agent|MultiEdit)\b[:\s]*(\S+)?",
                   snippet, _re.IGNORECASE)
    if not m:
        return ""
    tool = m.group(1).capitalize()
    arg = (m.group(2) or "")[:30].rstrip("/")
    if not arg:
        return tool
    return f"{tool}: {arg}"


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

    # MCP config — separated by surface so "unused" findings only judge
    # against the surface tokenmin actually scans (Code session history under
    # ~/.claude/projects/). See DETECTOR_RULES.md, rule: same-surface comparison.
    #
    # Code (Claude Code): mcpServers live in ~/.claude.json — top-level for
    #   user-wide servers, and `projects.<path>.mcpServers` for per-project
    #   overrides. Take the union of both.
    # Code (legacy): ~/.claude/mcp.json (rarely used, kept for back-compat).
    # Desktop (Claude Desktop): claude_desktop_config.json in either
    #   ~/.claude/ or ~/Library/Application Support/Claude/. NEVER mixed
    #   into mcp_servers — routed to mcp_servers_desktop_only so detectors
    #   that compare against Code session evidence ignore it.
    code_servers: set[str] = set()

    legacy_mcp = claude_home / "mcp.json"
    if legacy_mcp.exists():
        data = _safe_json(legacy_mcp) or {}
        servers = data.get("mcpServers") if isinstance(data, dict) else None
        if isinstance(servers, dict):
            code_servers.update(servers.keys())

    code_config = Path.home() / ".claude.json"
    if code_config.exists():
        data = _safe_json(code_config) or {}
        if isinstance(data, dict):
            top = data.get("mcpServers")
            if isinstance(top, dict):
                code_servers.update(top.keys())
            projects = data.get("projects")
            if isinstance(projects, dict):
                for proj_cfg in projects.values():
                    if not isinstance(proj_cfg, dict):
                        continue
                    proj_servers = proj_cfg.get("mcpServers")
                    if isinstance(proj_servers, dict):
                        code_servers.update(proj_servers.keys())

    snap.mcp_servers = sorted(code_servers)

    desktop_only: set[str] = set()
    for cand in [
        claude_home / "claude_desktop_config.json",
        Path.home() / "Library/Application Support/Claude/claude_desktop_config.json",
    ]:
        if cand.exists():
            data = _safe_json(cand) or {}
            servers = data.get("mcpServers") if isinstance(data, dict) else None
            if isinstance(servers, dict):
                # Exclude any server also loaded by Code — that one is not
                # "desktop-only" and would double-count.
                desktop_only.update(set(servers.keys()) - code_servers)

    snap.mcp_servers_desktop_only = sorted(desktop_only)

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
