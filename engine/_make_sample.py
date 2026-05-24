#!/usr/bin/env python3
"""Generate a synthetic ~/.claude tree and run CLIP against it to produce
clip/sample_output.md. Lets reviewers see the report shape without running
against real data. Throwaway helper — not part of the product surface.

SPDX-License-Identifier: Apache-2.0"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path


def _msg(role: str, content, model: str | None = None, usage: dict | None = None) -> dict:
    inner: dict = {"role": role, "content": content}
    if model:
        inner["model"] = model
    if usage:
        inner["usage"] = usage
    return {"type": role, "message": inner, "timestamp": _now_iso()}


def _now_iso(offset_s: float = 0.0) -> str:
    from datetime import datetime, timezone, timedelta
    return (datetime.now(tz=timezone.utc) - timedelta(seconds=offset_s)).isoformat().replace("+00:00", "Z")


def build_synthetic(claude_home: Path) -> None:
    projects = claude_home / "projects"
    projects.mkdir(parents=True, exist_ok=True)

    # ---- session 1: heavy file re-reads, lots of search, opus ----
    sess1 = projects / "-Users-rick-work-bigapp" / "abcd1234.jsonl"
    sess1.parent.mkdir(parents=True, exist_ok=True)
    lines: list[dict] = []
    for i in range(8):
        lines.append(_msg("user", [{"type": "text", "text": "now do the next thing"}]))
        tool_uses = []
        # 5 consecutive Grep calls = a long-search burst
        for _ in range(5):
            tool_uses.append({"type": "tool_use", "name": "Grep", "input": {"pattern": "TODO"}})
        # Re-read the same file 4 times across the session
        tool_uses.append({"type": "tool_use", "name": "Read", "input": {"file_path": "/Users/rick/work/bigapp/src/db/schema.ts"}})
        lines.append(
            _msg(
                "assistant",
                tool_uses + [{"type": "text", "text": "ok done"}],
                model="claude-opus-4-7",
                usage={"input_tokens": 12000, "output_tokens": 400, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 8000},
            )
        )
    with sess1.open("w") as f:
        for ln in lines:
            f.write(json.dumps(ln) + "\n")

    # ---- session 2: course-corrections galore, general-purpose agent abuse ----
    sess2 = projects / "-Users-rick-work-bigapp" / "efgh5678.jsonl"
    lines2: list[dict] = []
    redo_phrases = ["actually, do it the other way", "no wait, scratch that", "instead, use the v2 endpoint", "undo that change", "go back to the previous approach"]
    for ph in redo_phrases * 3:
        lines2.append(_msg("user", [{"type": "text", "text": ph}]))
        lines2.append(
            _msg(
                "assistant",
                [
                    {"type": "tool_use", "name": "Agent", "input": {"subagent_type": "general-purpose", "prompt": "find stuff"}},
                    {"type": "text", "text": "sure"},
                ],
                model="claude-opus-4-7",
                usage={"input_tokens": 5000, "output_tokens": 300},
            )
        )
    # add some plain user turns so the denominator is meaningful
    for _ in range(20):
        lines2.append(_msg("user", [{"type": "text", "text": "keep going"}]))
        lines2.append(
            _msg(
                "assistant",
                [{"type": "text", "text": "ok"}],
                model="claude-opus-4-7",
                usage={"input_tokens": 1500, "output_tokens": 200},
            )
        )
    with sess2.open("w") as f:
        for ln in lines2:
            f.write(json.dumps(ln) + "\n")

    # ---- session 3: permission denies (also exercises hook detector) ----
    sess3 = projects / "-Users-rick-work-other" / "ijkl9012.jsonl"
    sess3.parent.mkdir(parents=True, exist_ok=True)
    lines3: list[dict] = []
    for i in range(6):
        lines3.append(_msg("user", [{"type": "text", "text": "run the migration"}]))
        lines3.append(
            _msg(
                "assistant",
                [{"type": "tool_use", "name": "Bash", "input": {"command": "npm run migrate"}}],
                model="claude-sonnet-4-6",
                usage={"input_tokens": 2000, "output_tokens": 100},
            )
        )
        # permission denied tool_result
        lines3.append({"type": "tool_result", "content": "Permission denied for Bash", "timestamp": _now_iso()})
    with sess3.open("w") as f:
        for ln in lines3:
            f.write(json.dumps(ln) + "\n")

    # Create a global CLAUDE.md that is intentionally oversized AND mentions
    # known-obsolete features, so the new optimizer detectors fire.
    bloated_lines = ["# My CLAUDE.md", ""]
    bloated_lines.append("Use .claudeignore to skip vendor dirs.")
    bloated_lines.append("Set /effort 85 for hard problems.")
    bloated_lines.append("Run with claude --bare when scripting.")
    bloated_lines.append("")
    # pad to >200 lines
    for i in range(220):
        bloated_lines.append(f"- rule {i}: do the thing for case {i}")
    (claude_home / "CLAUDE.md").write_text("\n".join(bloated_lines), encoding="utf-8")
    # Settings file with no hooks so detect_no_hooks still fires.
    # (deliberately not creating agents/, mcp.json so those detectors fire too)


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        home = Path(td) / ".claude"
        build_synthetic(home)
        out = Path(__file__).parent / "sample_output.md"
        cmd = [sys.executable, str(Path(__file__).parent / "clip.py"),
               "--claude-home", str(home),
               "--days", "30",
               "--out", str(out)]
        r = subprocess.run(cmd, capture_output=True, text=True)
        sys.stdout.write(r.stdout)
        sys.stderr.write(r.stderr)
        return r.returncode


if __name__ == "__main__":
    raise SystemExit(main())
