#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Tokenmin. Open client CLI.

This is the open-source half of Tokenmin: it collects your Claude Code usage,
anonymizes it, and hands the anonymized snapshot to the Tokenmin engine. It holds
no detection rules of its own — findings come from the proprietary Tokenmin engine,
which runs either locally (a separately-installed closed module) or at the
hosted service. See LICENSING.md for the open/proprietary boundary.

Usage:
    python3 tokenmin.py --snapshot snap.json        # write anonymized snapshot, no engine
    python3 tokenmin.py --out report.md             # local engine (if installed) → report
    python3 tokenmin.py --submit-url URL --api-key K --out report.md   # hosted engine

Nothing leaves the machine unless you pass --submit-url. The anonymizer always
runs before the snapshot is written or sent (unless --no-anonymize, which is
local-debug only and refuses to submit). See README.md for the trust posture.
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import fields, is_dataclass
from pathlib import Path

# Make sibling modules importable when run as a script.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from analyzer import collect as collect_claude_code  # noqa: E402
from analyzer_chat_export import collect_from_export  # noqa: E402
from analyzer_desktop_native import collect_from_desktop_native  # noqa: E402
from anonymize import scrub_value, scrub_label, scrub_path, use_keep_suffix, selfcheck  # noqa: E402

# Built-in Claude Code agents; user-defined subagent_type values get hashed.
_BUILTIN_AGENTS = {"general-purpose", "statusline-setup", "output-style-setup"}


def _dataclass_to_dict(obj):
    """Best-effort JSON-able dump that handles Counter and set.

    Recurses field-by-field rather than via dataclasses.asdict(), because asdict
    rebuilds Counter fields by feeding (key, value) pairs to Counter(), which
    counts the pairs as elements and corrupts the data. Per-field recursion lets
    the Counter branch below convert each Counter correctly.
    """
    from collections import Counter

    if is_dataclass(obj):
        return {f.name: _dataclass_to_dict(getattr(obj, f.name)) for f in fields(obj)}
    if isinstance(obj, Counter):
        return dict(obj)
    if isinstance(obj, set):
        return sorted(obj)
    if isinstance(obj, dict):
        return {k: _dataclass_to_dict(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_dataclass_to_dict(v) for v in obj]
    return obj


def _local_engine():
    """Return the proprietary local engine if it's installed, else None.

    The engine is a separate, proprietary package. When present it exposes
    `analyze(snapshot: dict) -> str` returning the Markdown report. Its absence
    is normal — the open client is fully functional without it (it can still
    produce and submit the anonymized snapshot).
    """
    try:
        import tokenmin_engine  # type: ignore
    except ImportError:
        return None
    return getattr(tokenmin_engine, "analyze", None)


def _submit(url: str, api_key: str | None, snapshot: dict) -> str:
    """POST the anonymized snapshot to the hosted engine; return the report.

    HTTPS only — refuses http:// to prevent plaintext snapshot transmission.
    localhost / 127.0.0.1 over http is allowed for testing against the local
    server stub during F&F preview.
    """
    import urllib.parse
    import urllib.request

    parsed = urllib.parse.urlparse(url)
    is_local = parsed.hostname in ("localhost", "127.0.0.1", "::1")
    if parsed.scheme != "https" and not (parsed.scheme == "http" and is_local):
        raise SystemExit(
            f"tokenmin: refusing to submit to {parsed.scheme}://... — HTTPS required.\n"
            "(http:// is allowed only for localhost/127.0.0.1 during local testing.)"
        )

    payload = json.dumps({"snapshot": snapshot}).encode("utf-8")
    req = urllib.request.Request(url, data=payload, method="POST")
    req.add_header("Content-Type", "application/json")
    if api_key:
        req.add_header("Authorization", f"Bearer {api_key}")
    with urllib.request.urlopen(req) as resp:  # noqa: S310 (URL is user-supplied)
        body = resp.read().decode("utf-8")
    try:
        parsed = json.loads(body)
        if isinstance(parsed, dict) and "report" in parsed:
            return str(parsed["report"])
    except json.JSONDecodeError:
        pass
    return body


def _label_scrub_pass(snapshot: dict) -> dict:
    """Apply whole-string identifier hashing to known leak-prone fields.

    Run BEFORE scrub_value so the values are already-hashed when the
    free-text scrubber walks them. Targets:
      - sessions[*].project       (Claude Code project dir name)
      - sessions[*].agents_used   (custom subagent_type names)
      - sessions[*].tool_calls    (mcp__* tool names reveal integrated services)
      - config.mcp_servers
      - config.custom_agents, custom_skills, custom_commands
    """
    for s in snapshot.get("sessions") or []:
        if isinstance(s, dict):
            if s.get("project"):
                s["project"] = scrub_label(s["project"])
            au = s.get("agents_used") or {}
            if isinstance(au, dict):
                s["agents_used"] = {
                    (k if k in _BUILTIN_AGENTS else scrub_label(k)): v
                    for k, v in au.items()
                }
            tc = s.get("tool_calls") or {}
            if isinstance(tc, dict):
                s["tool_calls"] = {
                    (scrub_label(k) if k.startswith("mcp__") else k): v
                    for k, v in tc.items()
                }
            # Path fields: hash the WHOLE string. The regex-based path-scrub
            # leaks fragments when paths contain spaces (Google Drive mounts,
            # cloud-storage paths). Whole-string hash is leak-proof.
            fr = s.get("files_read") or {}
            if isinstance(fr, dict):
                s["files_read"] = {scrub_path(k): v for k, v in fr.items()}
            fw = s.get("files_written") or []
            if isinstance(fw, list):
                s["files_written"] = [scrub_path(x) for x in fw if isinstance(x, str)]
    cfg = snapshot.get("config") or {}
    if isinstance(cfg, dict):
        for key in ("mcp_servers", "custom_agents", "custom_skills", "custom_commands"):
            v = cfg.get(key) or []
            if isinstance(v, list):
                cfg[key] = [scrub_label(x) if isinstance(x, str) else x for x in v]
    return snapshot


def _emit_report(md: str, out: str | None) -> None:
    if out:
        Path(out).write_text(md, encoding="utf-8")
        print(f"tokenmin: wrote report to {out}", file=sys.stderr)
    else:
        sys.stdout.write(md)
        if not md.endswith("\n"):
            sys.stdout.write("\n")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="tokenmin",
        description="Claude Improvement Plan — open client: collect, anonymize, submit.",
    )
    p.add_argument(
        "--source",
        choices=("code", "export", "desktop-native"),
        default="code",
        help=(
            "Where to read usage from. "
            "'code' (default): Claude Code local sessions at --claude-home. "
            "'export': Anthropic chat export zip — works for both claude.ai and Claude Desktop. "
            "'desktop-native': Claude Desktop's live local store (not yet implemented; use 'export' instead)."
        ),
    )
    p.add_argument(
        "--from",
        dest="from_path",
        default=None,
        help="Path to the chat export (.zip, directory, or conversations.json) — required for --source export",
    )
    p.add_argument(
        "--claude-home",
        default=str(Path.home() / ".claude"),
        help="Path to .claude directory (default: ~/.claude) — used with --source code",
    )
    p.add_argument("--days", type=int, default=30, help="Lookback window in days (default: 30)")
    p.add_argument("--out", default=None, help="Write the report to this path (default: stdout)")
    p.add_argument(
        "--snapshot",
        default=None,
        help="Write the anonymized snapshot JSON to this path (the exact bytes the engine sees)",
    )
    p.add_argument(
        "--submit-url",
        default=None,
        help="Hosted Tokenmin engine endpoint. If set, the anonymized snapshot is POSTed here.",
    )
    p.add_argument(
        "--api-key",
        default=None,
        help=(
            "Bearer token for --submit-url. WARNING: visible in process list / shell history. "
            "Prefer --api-key-env."
        ),
    )
    p.add_argument(
        "--api-key-env",
        default=None,
        help="Name of an env var to read the bearer token from (recommended over --api-key).",
    )
    p.add_argument(
        "--no-anonymize",
        action="store_true",
        help="Skip anonymization (local debugging only — refuses to --submit-url)",
    )
    p.add_argument(
        "--keep-suffix",
        action="store_true",
        help="DEBUG: keep filename suffixes in hashed paths (legacy behavior; leaks suffix info across snapshots).",
    )
    p.add_argument(
        "--selfcheck",
        action="store_true",
        help="Dump a deterministic anonymization of sample inputs and exit. Use this to inspect the scrubber rules without reading code.",
    )
    args = p.parse_args(argv)

    if args.selfcheck:
        print(json.dumps(selfcheck(), indent=2))
        return 0

    use_keep_suffix(args.keep_suffix)

    api_key = args.api_key
    if args.api_key_env:
        import os as _os
        api_key = _os.environ.get(args.api_key_env)
        if not api_key:
            print(
                f"tokenmin: --api-key-env {args.api_key_env} is unset or empty.",
                file=sys.stderr,
            )
            return 4
    if args.api_key and not args.api_key_env:
        print(
            "tokenmin: warning: --api-key on the command line is visible in `ps` and shell history. "
            "Prefer --api-key-env VAR.",
            file=sys.stderr,
        )

    if args.no_anonymize and args.submit_url:
        print(
            "tokenmin: refusing to submit with --no-anonymize. Drop one of the flags.",
            file=sys.stderr,
        )
        return 3

    if args.source == "code":
        home = Path(args.claude_home).expanduser()
        if not home.exists():
            print(
                f"tokenmin: {home} does not exist. Have you used Claude Code on this machine?\n"
                f"If you use claude.ai or Claude Desktop instead, export your chats and run:\n"
                f"  tokenmin --source export --from path/to/export.zip",
                file=sys.stderr,
            )
            return 2
        snap = collect_claude_code(home, days=args.days)
    elif args.source == "export":
        if not args.from_path:
            print(
                "tokenmin: --source export requires --from PATH (the chat-export .zip).\n"
                "Get the export from claude.ai or Claude Desktop: Settings -> Export data.",
                file=sys.stderr,
            )
            return 2
        export_path = Path(args.from_path).expanduser()
        if not export_path.exists():
            print(f"tokenmin: {export_path} does not exist.", file=sys.stderr)
            return 2
        snap = collect_from_export(export_path, days=args.days)
    elif args.source == "desktop-native":
        snap = collect_from_desktop_native(None, days=args.days)
    else:  # argparse should prevent this
        print(f"tokenmin: unknown source {args.source!r}", file=sys.stderr)
        return 2
    snapshot = _dataclass_to_dict(snap)
    if not args.no_anonymize:
        # Two-pass scrub: label-hash known identifier fields first, then
        # free-text scrub the rest (paths, secrets, emails, IPs, ...).
        snapshot = _label_scrub_pass(snapshot)
        snapshot = scrub_value(snapshot)

    if args.snapshot:
        Path(args.snapshot).write_text(
            json.dumps(snapshot, indent=2, default=str), encoding="utf-8"
        )
        print(f"tokenmin: wrote anonymized snapshot to {args.snapshot}", file=sys.stderr)

    # Hand off to an engine to turn the snapshot into a report.
    if args.submit_url:
        _emit_report(_submit(args.submit_url, api_key, snapshot), args.out)
        return 0

    engine = _local_engine()
    if engine is not None:
        _emit_report(engine(snapshot), args.out)
        return 0

    # No engine available: the open client did its job (collect + anonymize).
    msg = (
        "tokenmin: anonymized snapshot ready"
        + (f" at {args.snapshot}" if args.snapshot else " (pass --snapshot PATH to save it)")
        + ".\nNo Tokenmin engine found. Install the local engine, or pass "
        "--submit-url to use the hosted service, to turn this snapshot into a report.\n"
        "The open client holds no detection rules — see LICENSING.md.\n"
    )
    print(msg, file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
