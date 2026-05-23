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

    claude = Path.home() / ".claude"
    if claude.exists():
        sess_dir = claude / "projects"
        n_sess = 0
        if sess_dir.is_dir():
            for p in sess_dir.iterdir():
                if p.is_dir():
                    n_sess += sum(1 for _ in p.glob("*.jsonl"))
        line("~/.claude", f"present ({n_sess} session files)", n_sess > 0)
    else:
        line("~/.claude", "MISSING — install Claude Code or use --source export", False)

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

    digest = _payload_digest(snapshot)
    _audit_log(
        "snapshot_built",
        source=args.source,
        days=args.days,
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

    if args.submit_url:
        _audit_log("submit_start", url=args.submit_url, sha256=digest)
        try:
            report = _submit(args.submit_url, api_key, snapshot)
        except Exception as exc:
            _audit_log("submit_error", url=args.submit_url, error=str(exc)[:200])
            raise
        _audit_log("submit_ok", url=args.submit_url, sha256=digest)
        _emit_report(report, args.out)
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
