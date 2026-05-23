# SPDX-License-Identifier: Apache-2.0
"""Native parser for Claude Desktop's local Electron store.

Status (2026-05-23): NOT BUILT, and on closer inspection, NOT WORTH BUILDING
as originally scoped. The export route (`tokenmin --source export`) is the
right answer for Desktop users.

Why — findings from reconnaissance on a real Claude Desktop install:

  1. Claude Desktop's "Code-in-Desktop" sessions (where the user runs Claude
     Code inside Desktop's window) are stored at
     `~/Library/Application Support/Claude/claude-code-sessions/<account>/<workspace>/local_<sid>.json`.
     Each file carries a `cliSessionId` field. The matching session JSONL
     ALSO exists at `~/.claude/projects/<projdir>/<cliSessionId>.jsonl` —
     identical content, same session ID. **The existing Claude Code adapter
     already captures these sessions.** A native Desktop adapter for
     Code-in-Desktop would double-count or duplicate work.

  2. Claude Desktop's chat-only mode (the regular conversational interface,
     same UX as claude.ai) keeps conversation bodies **server-side**. Locally
     cached: session metadata only — `model`, `effort`, MCP config, a turn
     `completedTurns` COUNT (an integer, not a list). Per-turn content and
     per-turn token usage are not in the local store.

  Consequence: even a perfect native parser of the local Electron store would
  surface only "N chat-mode sessions, average M turns, model X" — far less
  than the per-turn token / cache / tool-call data the engine needs for rich
  findings. Most current detectors would not fire usefully.

What works today, by user type:

  - Claude Code (CLI):              `tokenmin` (default; reads ~/.claude directly)
  - Code-in-Desktop:                same path; already covered by the CLI adapter
  - Chat-only Desktop or claude.ai: `tokenmin --source export --from <zip>`
                                    (Settings -> Privacy -> Export data, then
                                    `tokenmin help-export` for the walk-through)

When this might become worth building:

  - Anthropic ships a richer local cache for chat-mode (per-turn usage data)
  - Or Anthropic publishes a `/v1/conversations` style API we can pull from
  - Or we accept "thin findings" and ship the metadata-only adapter anyway
    (could surface "you use Opus 95% of the time in chat mode")

Until then, this module's job is to give Desktop users precise next-step
guidance when they invoke `--source desktop-native`, including the literal
local store paths on their platform.
"""
from __future__ import annotations

import platform
import sys
from pathlib import Path

from analyzer import Snapshot


_DESKTOP_STORE_HINTS = {
    "Darwin":  Path.home() / "Library" / "Application Support" / "Claude",
    "Linux":   Path.home() / ".config" / "Claude",
}


def _windows_store() -> Path | None:
    """%APPDATA%\\Claude on Windows."""
    import os
    appdata = os.environ.get("APPDATA")
    if appdata:
        return Path(appdata) / "Claude"
    return None


def desktop_store_path() -> Path | None:
    """The Electron store path Claude Desktop uses on this platform, if known."""
    system = platform.system()
    if system == "Windows":
        return _windows_store()
    return _DESKTOP_STORE_HINTS.get(system)


def collect_from_desktop_native(_unused: Path | None, days: int = 30) -> Snapshot:
    """Print precise next-step instructions and exit non-zero.

    Tells the user exactly what's in the local store on their platform and
    why the export route is the right answer — see the module docstring for
    the rationale. Reduces the 'why isn't this implemented yet?' friction.
    """
    store = desktop_store_path()
    msg = ["tokenmin: Claude Desktop's local store doesn't carry the data we need."]
    msg.append("")

    if store is not None:
        if store.exists():
            # Helpful detail: are there Code-in-Desktop sessions present?
            code_in_desktop = store / "claude-code-sessions"
            if code_in_desktop.is_dir():
                count = sum(1 for _ in code_in_desktop.rglob("local_*.json"))
                if count > 0:
                    msg.append(
                        f"  Found {count} Code-in-Desktop session file(s) at "
                        f"{code_in_desktop}/<acct>/<workspace>/local_*.json"
                    )
                    msg.append(
                        "  These ALSO live at ~/.claude/projects/.../<sid>.jsonl — "
                        "the regular `tokenmin` (--source code) already covers them."
                    )
                    msg.append("")
            msg.append(
                "  For chat-mode Desktop sessions, conversation bodies live on "
                "Anthropic's servers, not in the local store. The export route is "
                "the right answer."
            )
        else:
            msg.append(f"  No Desktop store found at: {store}")
            msg.append("  Is Claude Desktop installed? https://claude.ai/download")
    else:
        msg.append(f"  Unknown platform '{platform.system()}'.")

    msg += [
        "",
        "Workflow that works today:",
        "  1. `tokenmin help-export` walks the export step-by-step with a browser deep-link.",
        "  2. Settings -> Privacy -> Export data, then:",
        "       `tokenmin --source export --from latest`         picks newest in ~/Downloads",
        "       `tokenmin --source export --watch-downloads`     waits for it to arrive",
        "       `tokenmin demo`                                  see a sample report first",
        "",
        "If you also use Claude Code (CLI), the regular `tokenmin` already covers it.",
    ]
    raise SystemExit("\n".join(msg))
