# SPDX-License-Identifier: Apache-2.0
"""Native parser for Claude Desktop's local Electron store.

NOT YET IMPLEMENTED. Claude Desktop stores conversations in an Electron-managed
LevelDB / IndexedDB under (macOS) `~/Library/Application Support/Claude/`. The
format is not publicly documented and decoding it cleanly takes more than the
F&F preview budget.

Today, the path that works for Desktop users is:
  Settings -> Export data -> chat export zip
  tokenmin --source export --from path/to/export.zip --out report.md

That uses analyzer_chat_export.py, which Anthropic ships the same export
format for both claude.ai and Claude Desktop.

When this stub becomes a real adapter, it should produce a Snapshot with the
same schema, populated from the live local store (no manual export step).
"""
from __future__ import annotations

from pathlib import Path
import sys

from analyzer import Snapshot


# Hints for the eventual implementation — keep these around so the next
# session has a starting trail.
DESKTOP_STORE_HINTS = {
    "darwin": "~/Library/Application Support/Claude/",
    "win32": "%APPDATA%/Claude/",
    "linux": "~/.config/Claude/",
}


def collect_from_desktop_native(claude_desktop_home: Path | None, days: int = 30) -> Snapshot:
    raise SystemExit(
        "tokenmin: native Claude Desktop parsing is not implemented yet.\n"
        "for Desktop users today: export your chats from Settings -> Export data,\n"
        "then run: tokenmin --source export --from path/to/export.zip --out report.md\n"
    )
