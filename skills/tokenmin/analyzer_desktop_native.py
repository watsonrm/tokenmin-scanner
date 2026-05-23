# SPDX-License-Identifier: Apache-2.0
"""Native parser for Claude Desktop's local Electron store.

NOT YET IMPLEMENTED for live parsing. Claude Desktop stores conversations in an
Electron-managed LevelDB / IndexedDB under (macOS) `~/Library/Application
Support/Claude/`. Decoding it cleanly takes more than the F&F preview budget.

This module still does useful work today: it reports the store path it WOULD
parse, plus precise platform-specific export instructions so Desktop users have
a single clear next step.

When the live parser lands, replace `collect_from_desktop_native` here with the
real implementation that produces the same `Snapshot` schema. The CLI wiring
in `tokenmin.py` already routes `--source desktop-native` here.
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
    """Print precise next-step instructions, then exit non-zero.

    Rather than a generic 'not implemented' message, tell the user the literal
    path the store lives at on THEIR platform plus the exact menu path to
    trigger an export. Reduces 'what do I do' friction.
    """
    store = desktop_store_path()
    msg = ["tokenmin: native Claude Desktop parsing is not implemented yet."]
    if store is not None:
        if store.exists():
            msg.append(f"  detected Desktop store at: {store}")
            msg.append("  (the live parser will read this directly in a future release.)")
        else:
            msg.append(f"  no Desktop store found at: {store}")
            msg.append("  is Claude Desktop installed? https://claude.ai/download")
    else:
        msg.append(f"  unknown platform '{platform.system()}'.")

    msg += [
        "",
        "Workflow today (works for both Desktop and claude.ai):",
        "  1. Open Claude Desktop (or claude.ai in a browser).",
        "  2. Settings -> Privacy / Account -> Export data.",
        "  3. Wait for the export email, download the .zip.",
        "  4. tokenmin --source export --from path/to/claude-export-*.zip --out report.md",
    ]
    raise SystemExit("\n".join(msg))
