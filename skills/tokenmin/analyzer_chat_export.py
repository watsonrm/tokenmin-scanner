# SPDX-License-Identifier: Apache-2.0
"""Parse an Anthropic chat export into a Snapshot.

Same format ships from both claude.ai (Settings → Export data) and Claude Desktop
(Settings → Export data). Anthropic's export is a zip containing
`conversations.json` (an array of conversations, each with `chat_messages`).

This adapter is intentionally defensive about field naming because Anthropic's
export schema has changed at least twice in public memory. Anything we can't
parse counts toward `parse_errors` / `skipped_files` and is dropped silently.

Limits vs the Claude Code adapter: chat exports do NOT contain token counts,
model used per message, tool calls, or per-file reads. Most of the Snapshot's
quantitative fields stay zero. The engine still triggers behavioral findings
(redo signals, session length / no clear) but won't generate spend-based
recommendations. That's the honest ceiling for export-only data.
"""
from __future__ import annotations

import json
import time
import zipfile
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

# Reuse the schema + helpers from the Claude Code adapter.
from analyzer import (
    ConfigSnapshot,
    SessionStats,
    Snapshot,
    _REDO_HINTS,
)


# Anthropic has used at least these key names for the messages list.
_MESSAGE_KEYS = ("chat_messages", "messages")
# And these for the per-message text payload.
_MESSAGE_TEXT_KEYS = ("text", "content")
# And these for the sender role.
_MESSAGE_ROLE_KEYS = ("sender", "role")


def _read_export_blob(export_path: Path) -> Any:
    """Return the parsed conversations.json from either a zip or an extracted dir.

    Accepts:
      - path to the downloaded export zip
      - path to an already-extracted directory containing conversations.json
      - path to a conversations.json file directly
    """
    if export_path.is_file() and export_path.suffix.lower() == ".zip":
        with zipfile.ZipFile(export_path) as zf:
            for name in zf.namelist():
                if name.endswith("conversations.json"):
                    with zf.open(name) as f:
                        return json.load(f)
        raise FileNotFoundError(
            f"no conversations.json inside {export_path}"
        )
    if export_path.is_file() and export_path.suffix.lower() == ".json":
        # Any .json file: try parsing as a list of conversations.
        return json.loads(export_path.read_text(encoding="utf-8"))
    if export_path.is_dir():
        cand = export_path / "conversations.json"
        if cand.exists():
            return json.loads(cand.read_text(encoding="utf-8"))
    raise FileNotFoundError(
        f"could not find conversations.json at or inside {export_path}"
    )


def _epoch(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None


def _message_text(msg: dict) -> str:
    """Pull text out of however the export wraps it."""
    for k in _MESSAGE_TEXT_KEYS:
        v = msg.get(k)
        if isinstance(v, str):
            return v
        if isinstance(v, list):
            # Sometimes content is [{"type":"text","text":"..."}]
            parts: list[str] = []
            for b in v:
                if isinstance(b, dict):
                    t = b.get("text")
                    if isinstance(t, str):
                        parts.append(t)
                elif isinstance(b, str):
                    parts.append(b)
            return " ".join(parts)
    return ""


def _message_role(msg: dict) -> str:
    for k in _MESSAGE_ROLE_KEYS:
        v = msg.get(k)
        if isinstance(v, str):
            r = v.lower()
            if r in ("human", "user"):
                return "user"
            if r in ("assistant", "claude"):
                return "assistant"
    return ""


def _parse_conversation(conv: dict, cutoff: float | None, idx: int) -> SessionStats | None:
    """One conversation -> one SessionStats (chats are the closest analog to sessions)."""
    msgs: list[dict] = []
    for k in _MESSAGE_KEYS:
        v = conv.get(k)
        if isinstance(v, list):
            msgs = v
            break
    if not msgs:
        return None

    conv_id = conv.get("uuid") or conv.get("id") or f"export-{idx}"
    project = conv.get("name") or "(untitled)"
    stats = SessionStats(session_id=str(conv_id), project=str(project)[:120])

    started = _epoch(conv.get("created_at"))
    ended = _epoch(conv.get("updated_at"))
    if started is None and msgs:
        started = _epoch(msgs[0].get("created_at"))
    if ended is None and msgs:
        ended = _epoch(msgs[-1].get("created_at"))
    stats.started_at = started
    stats.ended_at = ended

    # Cutoff is applied conservatively: drop conversations whose latest activity
    # is before the cutoff. Conversations with no parseable timestamps survive.
    if cutoff is not None and ended is not None and ended < cutoff:
        return None

    for m in msgs:
        if not isinstance(m, dict):
            continue
        role = _message_role(m)
        text = _message_text(m)
        text_lc = text.lower()
        if role == "user":
            stats.user_turns += 1
            if any(h in text_lc for h in _REDO_HINTS):
                stats.redo_signals += 1
        elif role == "assistant":
            stats.assistant_turns += 1
            model = m.get("model") or m.get("model_name")
            if isinstance(model, str) and model:
                stats.models_used[model] += 1
            # Token counts almost never present in exports; if we ever see them,
            # fold them in. Otherwise the cost stays at $0 (honest).
            usage = m.get("usage") or {}
            if isinstance(usage, dict):
                stats.input_tokens += int(usage.get("input_tokens", 0) or 0)
                stats.output_tokens += int(usage.get("output_tokens", 0) or 0)

    if stats.user_turns == 0 and stats.assistant_turns == 0:
        return None
    return stats


def collect_from_export(export_path: Path, days: int = 30) -> Snapshot:
    """Build a Snapshot from an Anthropic chat export.

    `export_path` may be a .zip download, the extracted directory, or
    `conversations.json` directly.
    """
    cutoff = time.time() - days * 86400 if days > 0 else None
    sessions: list[SessionStats] = []
    parse_errors = 0
    skipped = 0

    try:
        conversations = _read_export_blob(export_path)
    except (FileNotFoundError, json.JSONDecodeError, zipfile.BadZipFile) as exc:
        raise SystemExit(
            f"tokenmin: could not read export at {export_path}: {exc}\n"
            "expected: a chat export .zip from claude.ai or Claude Desktop, "
            "or the extracted directory, or conversations.json directly."
        )

    if not isinstance(conversations, list):
        raise SystemExit(
            "tokenmin: conversations.json did not contain a list of conversations."
        )

    for idx, conv in enumerate(conversations):
        if not isinstance(conv, dict):
            skipped += 1
            continue
        try:
            s = _parse_conversation(conv, cutoff, idx)
            if s is None:
                skipped += 1
            else:
                sessions.append(s)
        except Exception:
            parse_errors += 1

    # The chat export tells us nothing about local config (it's per-machine).
    # Leave ConfigSnapshot at defaults so the engine knows it's missing data.
    config = ConfigSnapshot()
    return Snapshot(
        generated_at=time.time(),
        window_days=days,
        sessions=sessions,
        config=config,
        parse_errors=parse_errors,
        skipped_files=skipped,
    )
