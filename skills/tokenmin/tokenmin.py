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
import hashlib
import json
import os
import sys
import time
from dataclasses import fields, is_dataclass
from datetime import datetime, timezone
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


def _local_engine_structured():
    """Return the engine's analyze_structured(snapshot) -> dict, if available.

    Newer engine entry point used by the rich terminal renderer. Falls back to
    the markdown-only `analyze` via _local_engine() if unavailable.
    """
    try:
        import tokenmin_engine  # type: ignore
    except ImportError:
        return None
    return getattr(tokenmin_engine, "analyze_structured", None)


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


def _check_submit_url(url: str) -> None:
    """Refuse plaintext submission. Called early so the precondition fails fast
    before any data is collected. HTTPS-only except for localhost/127.0.0.1
    over http for local server-stub testing.
    """
    import urllib.parse
    parsed = urllib.parse.urlparse(url)
    is_local = parsed.hostname in ("localhost", "127.0.0.1", "::1")
    if parsed.scheme != "https" and not (parsed.scheme == "http" and is_local):
        raise SystemExit(
            f"tokenmin: refusing to submit to {parsed.scheme}://... — HTTPS required.\n"
            "(http:// is allowed only for localhost/127.0.0.1 during local testing.)"
        )


def _submit(url: str, api_key: str | None, snapshot: dict) -> str:
    """POST the anonymized snapshot to the hosted engine; return the report."""
    import urllib.request

    _check_submit_url(url)  # belt + suspenders; main() also calls this up front.
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


def _safe_write_json(path: Path, payload: dict, force: bool) -> None:
    """Write JSON to path with mode 0600. Refuses to overwrite without --force."""
    if path.exists() and not force:
        raise SystemExit(
            f"tokenmin: {path} already exists. Pass --force to overwrite, "
            "or pick a different --snapshot path."
        )
    data = json.dumps(payload, indent=2, default=str).encode("utf-8")
    # Atomic write at 0600.
    tmp = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(str(tmp), os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)
    os.replace(str(tmp), str(path))


def _audit_log(event: str, **fields_kv) -> None:
    """Append a JSON line to ~/.tokenmin/audit.log.

    Glasswing's "comprehensive logs" principle: the user should always be
    able to reconstruct what tokenmin did on their machine, when, and what
    bytes (by hash) it sent. The log is local, append-only, chmod 600.
    No user content is logged — just event metadata and digests.
    """
    log_dir = Path.home() / ".tokenmin"
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        return
    log_path = log_dir / "audit.log"
    record = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "event": event,
        **fields_kv,
    }
    line = (json.dumps(record) + "\n").encode("utf-8")
    try:
        fd = os.open(
            str(log_path),
            os.O_CREAT | os.O_APPEND | os.O_WRONLY,
            0o600,
        )
        try:
            os.write(fd, line)
        finally:
            os.close(fd)
    except OSError:
        # Logging failure is non-fatal.
        pass


def _payload_digest(payload: dict) -> str:
    """SHA-256 of the canonical JSON form of the payload. For audit log."""
    blob = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def _install_dir() -> Path:
    """Find the tokenmin install root (the bundle / scanner repo checkout)."""
    return Path(__file__).resolve().parent.parent.parent


def _version_info() -> dict:
    """Read version metadata from git + VERSION file if present."""
    root = _install_dir()
    info: dict[str, str] = {}
    vfile = root / "VERSION"
    if vfile.exists():
        info["version"] = vfile.read_text(encoding="utf-8").strip()
    try:
        import subprocess
        sha = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=False, timeout=2,
        ).stdout.strip()
        if sha:
            info["commit"] = sha[:12]
        date = subprocess.run(
            ["git", "-C", str(root), "log", "-1", "--format=%cI"],
            capture_output=True, text=True, check=False, timeout=2,
        ).stdout.strip()
        if date:
            info["commit_date"] = date
        remote = subprocess.run(
            ["git", "-C", str(root), "remote", "get-url", "origin"],
            capture_output=True, text=True, check=False, timeout=2,
        ).stdout.strip()
        if remote:
            info["remote"] = remote
    except Exception:
        pass
    return info


def _print_version() -> int:
    info = _version_info()
    if "version" in info:
        print(f"tokenmin {info['version']}")
    else:
        print(f"tokenmin (development build)")
    if "commit" in info:
        print(f"  commit:  {info['commit']}")
    if "commit_date" in info:
        print(f"  date:    {info['commit_date']}")
    if "remote" in info:
        print(f"  remote:  {info['remote']}")
    print(f"  install: {_install_dir()}")
    print(f"  python:  {sys.version.split()[0]}")
    return 0


def _doctor() -> int:
    """Self-diagnostic. Prints status of every component the installer touched."""
    import platform
    root = _install_dir()
    ok = True

    def line(label, value, good=True):
        nonlocal ok
        if not good:
            ok = False
        marker = "\033[32m✓\033[0m" if good else "\033[31m✗\033[0m"
        print(f"  {marker} {label:<32} {value}")

    print("tokenmin doctor — checking your install")
    print()

    line("python", sys.version.split()[0], sys.version_info >= (3, 10))
    line("platform", f"{platform.system()} {platform.release()}")
    line("install dir", str(root), root.exists())

    salt_path = Path.home() / ".tokenmin" / ".salt"
    salt_env = os.environ.get("TOKENMIN_SALT_PATH")
    if salt_env:
        salt_path = Path(salt_env).expanduser()
    salt_ok = salt_path.exists() and salt_path.stat().st_size >= 32
    mode = oct(salt_path.stat().st_mode & 0o777) if salt_path.exists() else "absent"
    line("salt file", f"{salt_path} ({mode})", salt_ok and (mode == "0o600" or not salt_path.exists() or salt_path.parent != Path.home() / ".tokenmin"))

    audit = Path.home() / ".tokenmin" / "audit.log"
    if audit.exists():
        size = audit.stat().st_size
        line("audit log", f"{audit} ({size} bytes)")
    else:
        line("audit log", f"{audit} (not yet created — runs once)")

    # Multi-Claude detection.
    claude = Path.home() / ".claude"
    if claude.exists():
        sess_dir = claude / "projects"
        n_sess = 0
        if sess_dir.is_dir():
            for p in sess_dir.iterdir():
                if p.is_dir():
                    n_sess += sum(1 for _ in p.glob("*.jsonl"))
        line("Claude Code", f"~/.claude ({n_sess} session files)", n_sess > 0)
    else:
        line("Claude Code", "not detected (~/.claude missing)", False)

    # Desktop store — platform-specific path
    if platform.system() == "Darwin":
        desktop_dir = Path.home() / "Library" / "Application Support" / "Claude"
    elif platform.system() == "Linux":
        desktop_dir = Path.home() / ".config" / "Claude"
    elif platform.system() == "Windows":
        desktop_dir = Path(os.environ.get("APPDATA", "")) / "Claude"
    else:
        desktop_dir = None

    if desktop_dir and desktop_dir.exists():
        line("Claude Desktop", str(desktop_dir))
    elif desktop_dir:
        line("Claude Desktop", f"not detected ({desktop_dir})")
    else:
        line("Claude Desktop", "not supported on this platform")

    # Engine availability
    engine_dir = root / "engine"
    engine_module = engine_dir / "tokenmin_engine.py"
    if engine_module.exists():
        line("engine", f"{engine_module.name} (F&F bundle)")
    else:
        line("engine", "not bundled (public scanner only — no reports without --submit-url)", True)

    # Auto-update setting
    au_mode = os.environ.get("TOKENMIN_AUTOUPDATE", "prompt")
    line("auto-update", f"{au_mode} (env: TOKENMIN_AUTOUPDATE)")

    # PATH check (best-effort — only reliable when invoked via the wrapper)
    bin_link = Path.home() / ".local" / "bin" / "tokenmin"
    if bin_link.exists():
        target = bin_link.resolve() if bin_link.is_symlink() else None
        line("symlink", f"{bin_link} -> {target}", target == (root / "tokenmin").resolve())
    else:
        line("symlink", f"{bin_link} (not found; you may be running tokenmin directly)")

    print()
    if ok:
        print("doctor: everything looks healthy.")
        return 0
    else:
        print("doctor: issues found above. fix them or rerun the installer.")
        return 1


def main(argv: list[str] | None = None) -> int:
    # Subcommands ('doctor', 'uninstall') come before the main argparser so they
    # have access to special-case behavior. Keep this loop simple.
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "doctor":
        return _doctor()
    if argv and argv[0] == "uninstall":
        return _uninstall(argv[1:])
    if argv and argv[0] in ("version", "--version", "-V"):
        return _print_version()
    if argv and argv[0] == "selftest":
        return _selftest()
    if argv and argv[0] in ("help", "-h", "--help"):
        # Custom onboarding help. argparse's --help is still available as `--help-argparse`.
        if argv[0] == "help" or len(argv) == 1:
            return _render_help()
    if argv and argv[0] == "show":
        if len(argv) < 2:
            print("usage: tokenmin show <finding-id>", file=sys.stderr)
            return 2
        return _render_show(argv[1])

    p = argparse.ArgumentParser(
        prog="tokenmin",
        description="Claude Improvement Plan — open client: collect, anonymize, submit.\n\n"
                    "Subcommands: doctor (self-diagnose), uninstall, version",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--version", "-V",
        action="store_true",
        help="Print version information and exit",
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
    p.add_argument(
        "--days",
        default="auto",
        help='Lookback window in days, or "auto" to adapt to data volume (default: auto)',
    )
    p.add_argument(
        "--out",
        default=None,
        help="Write full markdown report to this path. Default behavior is rich inline terminal output.",
    )
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
        help="DANGEROUS: skip anonymization. Local debugging only. Requires --i-know-what-im-doing.",
    )
    p.add_argument(
        "--i-know-what-im-doing",
        action="store_true",
        help="Second confirmation flag required for --no-anonymize.",
    )
    p.add_argument(
        "--force",
        action="store_true",
        help="Overwrite --snapshot and --out paths if they exist.",
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

    if args.version:
        return _print_version()

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

    if args.no_anonymize and not args.i_know_what_im_doing:
        print(
            "tokenmin: --no-anonymize disables the only thing protecting your\n"
            "data. If you really need a raw snapshot for debugging, also pass\n"
            "--i-know-what-im-doing. It still refuses to --submit-url.",
            file=sys.stderr,
        )
        return 3
    if args.no_anonymize and args.submit_url:
        print(
            "tokenmin: refusing to submit with --no-anonymize. Drop one of the flags.",
            file=sys.stderr,
        )
        return 3

    # Validate --submit-url BEFORE collecting any data, so an invalid scheme
    # fails fast and never touches the filesystem.
    if args.submit_url:
        _check_submit_url(args.submit_url)

    # Resolve --days. Accept "auto" (default) or an integer string. For code
    # source, peek at session count to auto-scale the window.
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
        days = _auto_days(home, args.days)
        _progress(f"scanning {home}", done=False)
        snap = collect_claude_code(home, days=days)
        _progress(f"found {len(snap.sessions)} sessions in last {days} days", done=True)
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
        try:
            days = int(args.days) if args.days != "auto" else 30
        except (TypeError, ValueError):
            days = 30
        _progress(f"reading export {export_path}", done=False)
        snap = collect_from_export(export_path, days=days)
        _progress(f"parsed {len(snap.sessions)} conversations", done=True)
    elif args.source == "desktop-native":
        snap = collect_from_desktop_native(None, days=30)
    else:
        print(f"tokenmin: unknown source {args.source!r}", file=sys.stderr)
        return 2

    snapshot = _dataclass_to_dict(snap)
    if not args.no_anonymize:
        _progress("anonymizing", done=False)
        snapshot = _label_scrub_pass(snapshot)
        snapshot = scrub_value(snapshot)
        _progress("anonymized", done=True)

    digest = _payload_digest(snapshot)
    _audit_log(
        "snapshot_built",
        source=args.source,
        days=snapshot.get("window_days"),
        anonymized=not args.no_anonymize,
        sessions=len(snapshot.get("sessions") or []),
        sha256=digest,
    )

    if args.snapshot:
        _safe_write_json(Path(args.snapshot), snapshot, force=args.force)
        print(
            f"tokenmin: wrote anonymized snapshot to {args.snapshot} (chmod 0600)",
            file=sys.stderr,
        )

    # --- hosted submit ------------------------------------------------------
    if args.submit_url:
        _audit_log("submit_start", url=args.submit_url, sha256=digest)
        try:
            _progress(f"submitting to {args.submit_url}", done=False)
            report = _submit(args.submit_url, api_key, snapshot)
            _progress("submitted", done=True)
        except Exception as exc:
            _audit_log("submit_error", url=args.submit_url, error=str(exc)[:200])
            raise
        _audit_log("submit_ok", url=args.submit_url, sha256=digest)
        _emit_report(report, args.out)
        return 0

    # --- local engine (preferred new path: analyze_structured) -------------
    structured_engine = _local_engine_structured()
    if structured_engine is not None:
        _progress("analyzing", done=False)
        result = structured_engine(snapshot)
        _progress("analyzed", done=True)
        _save_last_run(result)

        # Output mode: file = markdown; otherwise = rich inline.
        if args.out:
            _emit_report(result.get("report_md", ""), args.out)
        else:
            _render_terminal(result)
        return 0

    # --- fallback: legacy analyze() returning markdown only -----------------
    engine = _local_engine()
    if engine is not None:
        md = engine(snapshot)
        _emit_report(md, args.out)
        return 0

    # --- no engine: scanner-only mode --------------------------------------
    msg_lines = [
        "tokenmin: anonymized snapshot ready"
        + (f" at {args.snapshot}." if args.snapshot else " (pass --snapshot PATH to save it)."),
        "No Tokenmin engine found on this install — you're running scanner-only mode.",
        "",
        "  next:  tokenmin --snapshot snap.json     # see exactly what would be sent",
        "         tokenmin --submit-url HTTPS_URL   # hand off to a hosted engine",
        "",
        "The scanner holds no detection rules by design — see LICENSING.md.",
        "For the full report, ask Rick for an F&F invite at https://tokenmin.ai",
    ]
    print("\n".join(msg_lines), file=sys.stderr)
    return 0


# --- terminal rendering ----------------------------------------------------

# ANSI codes. Disabled automatically when stdout isn't a tty or NO_COLOR is set.
def _ansi_supported() -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    return sys.stdout.isatty()


class _C:
    """ANSI color/style codes, no-op when ANSI is unsupported."""
    def __init__(self, enable: bool):
        self.RESET = "\033[0m" if enable else ""
        self.BOLD = "\033[1m" if enable else ""
        self.DIM = "\033[2m" if enable else ""
        self.RED = "\033[31m" if enable else ""
        self.GREEN = "\033[32m" if enable else ""
        self.YELLOW = "\033[33m" if enable else ""
        self.BLUE = "\033[34m" if enable else ""
        self.MAGENTA = "\033[35m" if enable else ""
        self.CYAN = "\033[36m" if enable else ""
        self.GRAY = "\033[90m" if enable else ""


def _fmt_money(x: float) -> str:
    if x >= 1000:
        return f"${x:,.0f}"
    if x >= 10:
        return f"${x:.0f}"
    return f"${x:.2f}"


def _severity(savings: float) -> tuple[str, str, str]:
    """Returns (pill_text, color_code_attr, label) for a finding's $/mo."""
    if savings >= 500: return ("$$$$", "RED", "critical")
    if savings >= 100: return ("$$$", "YELLOW", "high")
    if savings >= 25:  return ("$$",  "CYAN", "medium")
    return ("$",  "GRAY", "low")


def _bar(ratio: float, width: int = 10) -> str:
    """Unicode bar showing 0..1 ratio over `width` cells."""
    ratio = max(0.0, min(1.0, ratio))
    filled = round(ratio * width)
    return "▮" * filled + "▯" * (width - filled)


# Lever / pillar labels for human-readable finding metadata.
_PILLAR_LABELS = {
    "1": "context discipline",
    "2": "model routing",
    "3": "parallelism / MCP",
    "4": "density of expression",
    "hygiene": "hygiene",
}

# Static comparison anchors per finding id. Keep terse; one-liners.
_ANCHORS: dict[str, str] = {
    "no_global_claude_md": "Anthropic recommends a global CLAUDE.md so Claude starts each project with your conventions loaded.",
    "oversized_claude_md": "Anthropic guidance: under 200 lines per CLAUDE.md. Past 200, adherence drops.",
    "obsolete_references": "These features don't exist. Claude will silently ignore them.",
    "long_sessions_no_clear": "Context window fills fast. Claude Code docs: use /clear between unrelated tasks; /compact near 50%.",
    "no_hooks": "Hooks let Claude react to events (SessionStart, file edits, permission denies).",
    "repeated_file_reads": "Each re-read costs ~3K tokens at sonnet input rates. Reference with @path or cache via CLAUDE.md hints.",
    "no_custom_agents": "Subagents run in their own context; verbose work never pollutes your main session.",
    "high_redo_signal": "Course-corrections mean Claude shipped plausible-but-wrong. Plan Mode + failing-test-first cuts the loop.",
    "long_searches": "Vague asks burn tokens on exploration. Add a 'where things live' map to CLAUDE.md.",
    "no_mcp": "MCP servers let Claude call external services natively, eliminating HTTP-explaining overhead.",
    "model_overspend": "Haiku is ~15x cheaper than Opus on input AND output. Route mechanical work to Haiku, complex reasoning to Opus.",
}


def _render_terminal(result: dict) -> None:
    """Print the inline headline card + ranked findings card. The 'magic moment'."""
    c = _C(_ansi_supported())
    snap = result.get("snapshot", {})
    findings = result.get("findings") or []
    total_save = result.get("total_savings_usd_per_month", 0.0)
    total_eff = result.get("total_hours_to_implement", 0.0)
    cost = snap.get("total_cost_usd", 0.0)
    sessions = snap.get("sessions", 0)
    days = snap.get("window_days", 0)
    models = snap.get("models") or []
    models_line = "  ·  ".join(f"{m['name']} {round(m['share']*100)}%" for m in models[:3]) or "no model data"

    line = "─" * 72

    # Header card.
    print()
    print(f"  {c.BOLD}{c.MAGENTA}Tokenmin{c.RESET}  Claude usage audit")
    print(f"  {c.GRAY}{line}{c.RESET}")
    print(f"  scanned {c.BOLD}{sessions}{c.RESET} sessions over {days} days")
    print(f"  est. spend (window): {c.BOLD}{_fmt_money(cost)}{c.RESET}")
    print(f"  model mix: {c.DIM}{models_line}{c.RESET}")
    print(f"  {c.GRAY}{line}{c.RESET}")
    print()

    if not findings:
        print(f"  {c.GREEN}✓{c.RESET} no findings — your setup looks clean")
        if sessions < 5:
            print(f"  {c.YELLOW}note: only {sessions} session(s) in window; rerun after more use{c.RESET}")
        _print_next_steps(c, [])
        return

    # Headline.
    print(f"  {c.BOLD}{c.YELLOW}Headline{c.RESET}  ~{c.BOLD}{_fmt_money(total_save)}/mo{c.RESET} recoverable across {len(findings)} fix(es), ~{total_eff:.1f} hrs total")
    print()

    max_save = max((f["savings_usd_per_month"] for f in findings), default=1.0) or 1.0

    for i, f in enumerate(findings, 1):
        save = f["savings_usd_per_month"]
        pill, color_attr, _ = _severity(save)
        pill_color = getattr(c, color_attr)
        rel = save / max_save if max_save > 0 else 0
        pillar = _PILLAR_LABELS.get(f.get("pillar", ""), "")
        conf = int(f.get("confidence", 0) * 100)
        hrs = f.get("hours_to_implement", 0.0)

        print(f"  {c.BOLD}{i}.{c.RESET} {c.BOLD}{f['title']}{c.RESET}")
        print(f"     {pill_color}{pill:<5}{c.RESET}  {_bar(rel)}  {c.BOLD}{_fmt_money(save)}/mo{c.RESET}  {c.DIM}{hrs:.1f} hrs · conf {conf}% · {pillar}{c.RESET}")
        print(f"     {c.DIM}evidence:{c.RESET} {f.get('evidence', '')}")
        print(f"     {c.CYAN}→{c.RESET} {c.DIM}tokenmin show {f['id']}{c.RESET}")
        print()

    _print_next_steps(c, findings)


def _print_next_steps(c: "_C", findings: list) -> None:
    print(f"  {c.GRAY}─" * 36 + f"{c.RESET}")
    print(f"  next steps:")
    if findings:
        print(f"    {c.BOLD}tokenmin show <id>{c.RESET}    drill into one finding")
    print(f"    {c.BOLD}tokenmin --out report.md{c.RESET}  write the full markdown report")
    print(f"    {c.BOLD}tokenmin help{c.RESET}             30-second walkthrough")
    print(f"    {c.GRAY}guide: https://tokenmin.ai/guides/claude-token-optimization{c.RESET}")
    print()


def _render_show(finding_id: str) -> int:
    """Drill-down into one finding. Reads last_run.json."""
    c = _C(_ansi_supported())
    last = _load_last_run()
    if not last:
        print("tokenmin show: no recent run found.", file=sys.stderr)
        print("  run `tokenmin` first to produce findings, then `tokenmin show <id>`.", file=sys.stderr)
        return 2
    findings = last.get("findings") or []
    found = next((f for f in findings if f["id"] == finding_id), None)
    if not found:
        print(f"tokenmin show: no finding with id '{finding_id}' in last run.", file=sys.stderr)
        if findings:
            print("  available findings:", file=sys.stderr)
            for f in findings:
                print(f"    {f['id']}", file=sys.stderr)
        return 2

    save = found["savings_usd_per_month"]
    pill, color_attr, label = _severity(save)
    pill_color = getattr(c, color_attr)
    pillar = _PILLAR_LABELS.get(found.get("pillar", ""), "hygiene")
    conf = int(found.get("confidence", 0) * 100)
    hrs = found.get("hours_to_implement", 0.0)

    print()
    print(f"  {c.BOLD}{c.MAGENTA}{found['title']}{c.RESET}")
    print(f"  {c.GRAY}finding id: {found['id']}{c.RESET}")
    print()
    print(f"  Severity  {pill_color}{pill}{c.RESET} ({label})")
    print(f"  Impact    {c.BOLD}{_fmt_money(save)}/mo{c.RESET} estimated savings")
    print(f"  Effort    {hrs:.1f} hrs to implement")
    print(f"  Pillar    {pillar}")
    print(f"  Confidence {conf}%")
    print()
    print(f"  {c.BOLD}Evidence{c.RESET}")
    print(f"  {found.get('evidence', '')}")
    anchor = _ANCHORS.get(found["id"])
    if anchor:
        print()
        print(f"  {c.BOLD}Why this matters{c.RESET}")
        print(f"  {c.DIM}{anchor}{c.RESET}")
    print()
    print(f"  {c.BOLD}How to fix{c.RESET}")
    # Render how_to_fix as-is (it's already markdown-flavored copy).
    for line in (found.get("how_to_fix", "") or "").splitlines():
        print(f"  {line}")
    print()
    return 0


def _render_help() -> int:
    """30-second walkthrough that replaces the bare argparse dump."""
    c = _C(_ansi_supported())
    print()
    print(f"  {c.BOLD}{c.MAGENTA}Tokenmin{c.RESET}  Claude usage advisor")
    print()
    print(f"  Reads how you use Claude, anonymizes it, and shows you what to fix next.")
    print()
    print(f"  {c.BOLD}First run{c.RESET}")
    print(f"    {c.CYAN}tokenmin{c.RESET}                      scan, anonymize, show findings inline")
    print()
    print(f"  {c.BOLD}Drill into one finding{c.RESET}")
    print(f"    {c.CYAN}tokenmin show <id>{c.RESET}            full evidence + the fix")
    print()
    print(f"  {c.BOLD}Variants{c.RESET}")
    print(f"    {c.CYAN}tokenmin --source export --from FILE{c.RESET}    audit claude.ai or Desktop export")
    print(f"    {c.CYAN}tokenmin --out report.md{c.RESET}                write full markdown report")
    print(f"    {c.CYAN}tokenmin --snapshot snap.json{c.RESET}           inspect anonymized payload")
    print(f"    {c.CYAN}tokenmin --submit-url URL{c.RESET}               send to hosted engine (HTTPS only)")
    print()
    print(f"  {c.BOLD}Maintenance{c.RESET}")
    print(f"    {c.CYAN}tokenmin --version{c.RESET}            what you're running")
    print(f"    {c.CYAN}tokenmin doctor{c.RESET}               self-diagnose")
    print(f"    {c.CYAN}tokenmin selftest{c.RESET}             run the bundled tests")
    print(f"    {c.CYAN}tokenmin uninstall{c.RESET}            clean removal")
    print()
    print(f"  {c.BOLD}What gets collected and what doesn't{c.RESET}")
    print(f"    {c.GRAY}collected (hashed):{c.RESET} file paths, project names, MCP server names,")
    print(f"      custom agent/skill/command names, model names, token counts, timestamps")
    print(f"    {c.GRAY}never collected:{c.RESET} message text, tool outputs, anything outside ~/.claude/")
    print()
    print(f"  {c.BOLD}Trust posture{c.RESET}")
    print(f"    Apache-2.0 scanner: github.com/watsonrm/tokenmin-scanner")
    print(f"    Threat model + disclosure: SECURITY.md in that repo")
    print(f"    {c.CYAN}tokenmin --selfcheck{c.RESET}          verify the anonymizer rules")
    print()
    print(f"  Full guide: {c.BLUE}https://tokenmin.ai/guides/claude-token-optimization{c.RESET}")
    print()
    return 0


# --- last-run cache (for tokenmin show) ------------------------------------

def _last_run_path() -> Path:
    return Path.home() / ".tokenmin" / "last_run.json"


def _save_last_run(result: dict) -> None:
    """Persist the structured run for later `tokenmin show <id>`."""
    path = _last_run_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "saved_at": datetime.now(timezone.utc).isoformat(),
        "snapshot": result.get("snapshot", {}),
        "findings": result.get("findings", []),
        "total_savings_usd_per_month": result.get("total_savings_usd_per_month", 0.0),
        "engine_version": result.get("engine_version", ""),
    }
    data = json.dumps(payload, indent=2, default=str).encode("utf-8")
    tmp = path.with_suffix(".tmp")
    fd = os.open(str(tmp), os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)
    os.replace(str(tmp), str(path))


def _load_last_run() -> dict | None:
    try:
        return json.loads(_last_run_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


# --- progress indicator ----------------------------------------------------

def _progress(msg: str, done: bool = False) -> None:
    """Print a progress line to stderr. Skipped in quiet mode (--out, --snapshot, --submit-url paths can suppress)."""
    if os.environ.get("TOKENMIN_QUIET") == "1":
        return
    marker = "✓" if done else "▶"
    c = _C(_ansi_supported())
    print(f"  {c.DIM}{marker} {msg}{c.RESET}", file=sys.stderr)


# --- smart defaults --------------------------------------------------------

def _auto_days(claude_home: Path, requested: str | int) -> int:
    """Adaptive window based on data volume. Override with --days N.

    Scales:
      - <  5 .jsonl files       -> 90 days (cast a wide net)
      - 5-50 files              -> 30 days (default)
      - > 50 files              -> 14 days (recent only)
    """
    if requested != "auto":
        return int(requested)
    proj_dir = claude_home / "projects"
    if not proj_dir.is_dir():
        return 30
    n = sum(1 for _ in proj_dir.rglob("*.jsonl"))
    if n < 5:
        return 90
    if n > 50:
        return 14
    return 30


def _selftest() -> int:
    """Run the bundled property + CLI tests against the installed tree.

    Same suite CI runs on every push. Catches "is your install actually
    intact?" — useful after manual edits, partial pulls, or filesystem
    corruption.
    """
    import subprocess
    root = _install_dir()
    runner = root / "tests" / "run.sh"
    if not runner.exists():
        print(
            f"tokenmin selftest: tests not present at {runner}.\n"
            "(scanner builds without tests are unusual — re-run the installer.)",
            file=sys.stderr,
        )
        return 2
    print(f"tokenmin selftest: running {runner}")
    res = subprocess.run(["bash", str(runner)], cwd=str(root))
    return res.returncode


def _uninstall(args: list[str]) -> int:
    """Remove the install dir + symlink. Prompts before deleting state."""
    import argparse as _ap
    sp = _ap.ArgumentParser(prog="tokenmin uninstall", description="Remove tokenmin from this machine.")
    sp.add_argument("--keep-state", action="store_true", help="Keep ~/.tokenmin/.salt and audit.log (default removes them)")
    sp.add_argument("--yes", "-y", action="store_true", help="Skip the confirmation prompt")
    a = sp.parse_args(args)

    root = _install_dir()
    bin_link = Path.home() / ".local" / "bin" / "tokenmin"
    state_dir = Path.home() / ".tokenmin"

    print("tokenmin uninstall will remove:")
    print(f"  install dir:  {root}")
    if bin_link.is_symlink():
        print(f"  symlink:      {bin_link}")
    if not a.keep_state and state_dir.exists() and state_dir != root:
        print(f"  state dir:    {state_dir}  (salt, audit log)")
    if state_dir == root:
        print(f"  (state lives inside install dir; --keep-state has no effect)")

    if not a.yes:
        if not sys.stdin.isatty():
            print("tokenmin uninstall: refusing non-interactive run without --yes", file=sys.stderr)
            return 2
        ans = input("proceed? [y/N] ").strip().lower()
        if ans not in ("y", "yes"):
            print("aborted.")
            return 1

    import shutil
    # Remove symlink if it points at us.
    if bin_link.is_symlink():
        try:
            target = bin_link.resolve()
            if target == (root / "tokenmin").resolve():
                bin_link.unlink()
                print(f"removed symlink: {bin_link}")
            else:
                print(f"left {bin_link} alone (points at {target}, not us)")
        except OSError as exc:
            print(f"warning: could not remove {bin_link}: {exc}", file=sys.stderr)

    # Remove install dir.
    if root.exists():
        try:
            shutil.rmtree(root)
            print(f"removed install dir: {root}")
        except OSError as exc:
            print(f"error: could not remove {root}: {exc}", file=sys.stderr)
            return 3

    # State dir cleanup (only if separate from install dir).
    if not a.keep_state and state_dir.exists() and state_dir != root:
        try:
            shutil.rmtree(state_dir)
            print(f"removed state dir:   {state_dir}")
        except OSError as exc:
            print(f"warning: could not remove {state_dir}: {exc}", file=sys.stderr)

    print()
    print("uninstalled. you may also want to remove the PATH line the installer added")
    print("to your shell rc (~/.zshrc / ~/.bashrc / ~/.config/fish/config.fish).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
