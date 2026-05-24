#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Tokenmin CLI.

Collects your Claude Code usage, anonymizes it, and hands the anonymized
snapshot to the Tokenmin engine (which ships in `engine/` next door, Apache-2.0
like the rest of this repo). See LICENSING.md for the file layout.

Usage:
    python3 tokenmin.py --snapshot snap.json        # write anonymized snapshot, no engine
    python3 tokenmin.py --out report.md             # local engine → report
    python3 tokenmin.py --submit-url URL --api-key K --out report.md   # hosted endpoint (future)

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
    """Return the local engine's `analyze` entry point if it's importable, else None.

    The engine ships at `engine/` in this repo (Apache-2.0). Auto-added to
    sys.path at module load. When present it exposes
    `analyze(snapshot: dict) -> str` returning the Markdown report. Its absence
    is unusual and usually means a broken install — the scanner still functions
    (it can produce + submit the anonymized snapshot) but won't render reports.
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


def _update_status(force_refresh: bool = False, timeout_sec: int = 3) -> dict:
    """Check whether a newer tokenmin is available on origin/main.

    Returns dict with keys: current_version, current_sha, latest_version,
    latest_sha, up_to_date, checked_at, error.

    Result cached in ~/.tokenmin/.update-status for 1 hour (forced refresh
    bypasses cache). Skips silently on no-network / no-git / dirty errors —
    error is non-empty when the check itself failed but never raises.
    """
    import subprocess as _sp
    root = _install_dir()
    cache = root / ".update-status"
    info = _version_info()
    current_version = info.get("version", "dev")
    current_sha = info.get("commit", "")  # truncated to 12 chars by _version_info

    def _shas_equal(a: str, b: str) -> bool:
        # `_version_info` returns 12-char short SHA; `git ls-remote` returns
        # the 40-char full SHA. Prefix-compare so they agree.
        if not a or not b:
            return False
        n = min(len(a), len(b))
        return a[:n] == b[:n]

    # Cache check — short-circuit when not forced and within 1h. Refresh the
    # `current_*` fields from disk on every read so an update doesn't leave
    # the cache reporting stale "you're behind" forever.
    if not force_refresh and cache.is_file():
        try:
            cached = json.loads(cache.read_text(encoding="utf-8"))
            ts = cached.get("checked_at", "")
            if ts:
                age = (datetime.now(timezone.utc) - datetime.fromisoformat(ts)).total_seconds()
                if age < 3600:
                    cached["current_version"] = current_version
                    cached["current_sha"] = current_sha
                    cached["up_to_date"] = _shas_equal(cached.get("latest_sha") or "", current_sha)
                    return cached
        except (OSError, json.JSONDecodeError, ValueError):
            pass

    result = {
        "current_version": current_version,
        "current_sha": current_sha,
        "latest_version": None,
        "latest_sha": None,
        "up_to_date": None,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "error": "",
    }

    if not (root / ".git").is_dir():
        result["error"] = "not a git repo (scanner-only install or manually copied)"
        return result
    try:
        # ls-remote is read-only and doesn't change the working tree.
        proc = _sp.run(
            ["git", "-C", str(root), "ls-remote", "--quiet", "origin", "main"],
            capture_output=True, text=True, timeout=timeout_sec,
        )
        if proc.returncode != 0 or not proc.stdout.strip():
            result["error"] = "ls-remote failed (offline? auth?)"
            return result
        remote_sha = proc.stdout.split()[0]
    except (OSError, _sp.TimeoutExpired) as exc:
        result["error"] = f"git error: {exc}"
        return result

    result["latest_sha"] = remote_sha
    result["up_to_date"] = _shas_equal(remote_sha, current_sha)

    # Fetch the VERSION file from the remote ref (without merging). Use
    # cat-file with --no-pager-style direct read.
    try:
        # Need the remote object first — fetch it shallow.
        _sp.run(
            ["git", "-C", str(root), "fetch", "--quiet", "--depth", "1", "origin", remote_sha],
            capture_output=True, timeout=timeout_sec,
        )
        proc = _sp.run(
            ["git", "-C", str(root), "show", f"{remote_sha}:VERSION"],
            capture_output=True, text=True, timeout=timeout_sec,
        )
        if proc.returncode == 0:
            result["latest_version"] = proc.stdout.strip()
    except (OSError, _sp.TimeoutExpired):
        pass

    try:
        cache.write_text(json.dumps(result, indent=2), encoding="utf-8")
    except OSError:
        pass
    return result


def _print_version() -> int:
    info = _version_info()
    c = _C(_ansi_supported())
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

    # v0.12.5: surface "you're behind" right here so users don't have to dig.
    # Best-effort: skips silently on no-network.
    try:
        status = _update_status()
        if status.get("up_to_date") is False:
            latest = status.get("latest_version") or "newer"
            latest_sha = (status.get("latest_sha") or "")[:7]
            print()
            print(f"  {c.YELLOW}update available:{c.RESET} {c.BOLD}{latest}{c.RESET} "
                  f"({latest_sha}) — run {c.CYAN}tokenmin update{c.RESET}")
        elif status.get("up_to_date") is True:
            print(f"  status:  {c.GREEN}up to date{c.RESET}")
    except Exception:
        pass
    return 0


def _update_cmd(args: list[str]) -> int:
    """tokenmin update — explicit, immediate, bypasses the 24h cooldown.

    Reports old SHA → new SHA + version. Refuses on a dirty tree (the user
    has local edits) or when TOKENMIN_AUTOUPDATE=off. Honors
    TOKENMIN_REQUIRE_SIGNED=1 the same way the bash wrapper does.
    """
    import argparse as _ap
    import subprocess as _sp
    sp = _ap.ArgumentParser(prog="tokenmin update", description="Pull the latest tokenmin into ~/.tokenmin.")
    sp.add_argument("--check", action="store_true", help="Check only; don't pull")
    a = sp.parse_args(args)
    c = _C(_ansi_supported())
    root = _install_dir()

    if os.environ.get("TOKENMIN_AUTOUPDATE", "").lower() == "off" and not a.check:
        print(f"{c.YELLOW}!{c.RESET} TOKENMIN_AUTOUPDATE=off — update refused.", file=sys.stderr)
        print(f"  unset the env var or run with `TOKENMIN_AUTOUPDATE=auto tokenmin update`.", file=sys.stderr)
        return 1

    if not (root / ".git").is_dir():
        print(f"{c.YELLOW}!{c.RESET} {root} isn't a git checkout; can't update.", file=sys.stderr)
        print(f"  reinstall: curl --proto '=https' --tlsv1.2 -fsSL https://tokenmin.ai/install.sh | bash", file=sys.stderr)
        return 1

    # Force-refresh status (bypass cache).
    status = _update_status(force_refresh=True)
    if status.get("error"):
        print(f"{c.YELLOW}!{c.RESET} update check failed: {status['error']}", file=sys.stderr)
        return 1
    current_sha = status.get("current_sha", "")
    latest_sha = status.get("latest_sha", "")
    latest_version = status.get("latest_version") or "?"
    current_version = status.get("current_version") or "?"

    if status.get("up_to_date"):
        print(f"{c.GREEN}✓{c.RESET} tokenmin {current_version} is the latest. Nothing to do.")
        return 0

    if a.check:
        print(f"  current: {c.BOLD}{current_version}{c.RESET} ({current_sha[:7]})")
        print(f"  latest:  {c.BOLD}{latest_version}{c.RESET} ({latest_sha[:7]})")
        print(f"  run {c.CYAN}tokenmin update{c.RESET} to apply.")
        return 0

    # Refuse if working tree is dirty.
    try:
        dirty = _sp.run(
            ["git", "-C", str(root), "status", "--porcelain"],
            capture_output=True, text=True, timeout=5,
        )
        if dirty.returncode == 0 and dirty.stdout.strip():
            print(f"{c.YELLOW}!{c.RESET} working tree at {root} has local changes; refusing to update.", file=sys.stderr)
            print(f"  inspect with `cd {root} && git status` and reset before retrying.", file=sys.stderr)
            return 1
    except (OSError, _sp.TimeoutExpired):
        pass

    # Optional signature verification (matches the bash wrapper's logic).
    if os.environ.get("TOKENMIN_REQUIRE_SIGNED", "").lower() in ("1", "on", "true", "yes"):
        try:
            verify = _sp.run(
                ["git", "-C", str(root), "log", "-1", "--pretty=%G?", latest_sha],
                capture_output=True, text=True, timeout=5,
            )
            v = verify.stdout.strip()
            if v not in ("G", "U"):
                print(f"{c.YELLOW}!{c.RESET} commit {latest_sha[:7]} signature did not verify (status={v}); refusing.", file=sys.stderr)
                print(f"  unset TOKENMIN_REQUIRE_SIGNED to allow unsigned updates.", file=sys.stderr)
                return 1
        except (OSError, _sp.TimeoutExpired):
            pass

    print(f"  pulling {current_sha[:7]} → {latest_sha[:7]}...")
    try:
        pull = _sp.run(
            ["git", "-C", str(root), "merge", "--ff-only", "--quiet", latest_sha],
            capture_output=True, text=True, timeout=15,
        )
    except (OSError, _sp.TimeoutExpired) as exc:
        print(f"{c.YELLOW}!{c.RESET} merge failed: {exc}", file=sys.stderr)
        return 1
    if pull.returncode != 0:
        print(f"{c.YELLOW}!{c.RESET} merge --ff-only failed:", file=sys.stderr)
        print(pull.stderr or pull.stdout, file=sys.stderr)
        return 1

    # Reset the wrapper's 24h cooldown so it doesn't double-update next run.
    stamp = root / ".last-update-check"
    try:
        stamp.write_text(str(int(datetime.now(timezone.utc).timestamp())))
    except OSError:
        pass
    # Clear the status cache so the next --version check sees fresh state.
    try:
        (root / ".update-status").unlink()
    except OSError:
        pass

    # Re-read VERSION post-merge so the success line is accurate.
    new_version = current_version
    try:
        vfile = root / "VERSION"
        if vfile.is_file():
            new_version = vfile.read_text(encoding="utf-8").strip()
    except OSError:
        pass
    print(f"{c.GREEN}✓{c.RESET} tokenmin updated to {c.BOLD}{new_version}{c.RESET} ({latest_sha[:7]})")
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
        line("engine", f"{engine_module.name}")
    else:
        line("engine", "not found (expected at engine/tokenmin_engine.py)", True)

    # Auto-update setting + actual update status (v0.12.5).
    au_mode = os.environ.get("TOKENMIN_AUTOUPDATE", "prompt")
    line("auto-update", f"{au_mode} (env: TOKENMIN_AUTOUPDATE)")
    # Best-effort status check — silent on network failure.
    try:
        st = _update_status()
        if st.get("up_to_date") is True:
            line("update status", "up to date")
        elif st.get("up_to_date") is False:
            latest = st.get("latest_version") or "newer"
            line("update status", f"{latest} available — run `tokenmin update`", False)
        else:
            err = st.get("error") or "unknown"
            line("update status", f"check skipped ({err})")
    except Exception:
        pass

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
    if argv and argv[0] == "watch":
        return _watch(argv[1:])
    if argv and argv[0] == "demo":
        return _demo()
    if argv and argv[0] == "help-export":
        return _help_export()
    if argv and argv[0] == "telemetry":
        return _telemetry_cmd(argv[1:])
    if argv and argv[0] == "plan":
        return _plan_cmd(argv[1:])
    if argv and argv[0] == "update":
        return _update_cmd(argv[1:])

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
        help='Path to the chat export (.zip, directory, or conversations.json). Pass "latest" to auto-pick the newest Anthropic export zip in ~/Downloads/. Required for --source export unless --watch-downloads is set.',
    )
    p.add_argument(
        "--watch-downloads",
        action="store_true",
        help="With --source export: poll ~/Downloads/ for a new Anthropic export zip and run automatically when one arrives. Ctrl-C exits.",
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

    # First-run telemetry consent (no-op when already decided, or non-interactive,
    # or F&F-pre-configured). Never asks for runs that won't produce findings.
    _maybe_telemetry_consent()
    # First-run billing plan consent — lets the report frame savings as quota
    # stretch instead of dollar savings for flat-fee Pro/Max users.
    _maybe_billing_plan_consent()
    # If pricing.json is older than its stale threshold, warn ONCE so users
    # know dollar numbers may not match Anthropic's current published rates.
    _maybe_stale_pricing_warning()

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
        # --watch-downloads short-circuits the main flow: it polls, then runs.
        if args.watch_downloads:
            return _watch_downloads_for_export(args.out, args.force)
        if not args.from_path:
            print(
                "tokenmin: --source export needs either --from PATH or --watch-downloads.\n"
                "  --from latest                      auto-pick newest Anthropic export in ~/Downloads/\n"
                "  --from path/to/export.zip          explicit\n"
                "  --watch-downloads                  wait for the next export to arrive\n"
                "How to export: tokenmin help-export",
                file=sys.stderr,
            )
            return 2
        # Resolve --from latest.
        if args.from_path == "latest":
            latest = _find_latest_export()
            if latest is None:
                print(
                    "tokenmin: --from latest: no Anthropic export zip found in ~/Downloads/.\n"
                    "  trigger one at https://claude.ai/settings/data-privacy-controls then\n"
                    "  rerun (or use --watch-downloads to wait for it automatically).",
                    file=sys.stderr,
                )
                return 2
            export_path = latest
            print(f"  --from latest -> {export_path.name}", file=sys.stderr)
        else:
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
        # Stash export path on the args object for the delete-after prompt.
        args._export_source_path = export_path
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
        # Pass the billing plan so the report frames savings in the right unit
        # (dollar savings on API; % quota stretch on flat-fee Pro/Max).
        try:
            result = structured_engine(snapshot, billing_plan=_billing_plan())
        except TypeError:
            # Pre-0.5 engine without billing_plan kwarg.
            result = structured_engine(snapshot)
        _progress("analyzed", done=True)
        _save_last_run(result)

        # Output mode: file = markdown; otherwise = rich inline.
        if args.out:
            _emit_report(result.get("report_md", ""), args.out)
        else:
            _render_terminal(result)

        # Telemetry: send the event if enabled. Silent on every failure mode.
        # The discovery fields (metrics + setup_signature) feed empirical
        # detection of NEW optimization patterns — see SECURITY.md.
        if _telemetry_enabled():
            try:
                snap_info = result.get("snapshot") or {}
                # Use raw integer counts, not shares (issue scanner#1):
                # shares are floats in [0,1] that would int-floor to 0 for
                # anything <100%. Counts are more analytically useful for
                # the discovery layer anyway.
                families = {}
                for m in snap_info.get("models") or []:
                    families[m["name"].lower()] = int(m.get("count", 0))
                # Compute avg_tools_per_turn from the snapshot if not surfaced.
                snapshot_summary = dict(snap_info)
                # Best-effort avg-tools-per-turn from the raw snapshot dict.
                snapshot_summary.setdefault("avg_tools_per_turn", None)
                event = _build_telemetry_event(
                    subcommand="run",
                    findings_fired=[f["id"] for f in (result.get("findings") or [])],
                    session_count=snap_info.get("sessions", 0),
                    models_used_families=families,
                    snapshot_summary=snapshot_summary,
                    config_summary=snap_info.get("config") or {},
                )
                _send_telemetry(event)
            except Exception:
                pass

        # For export-mode runs, offer to delete the source zip so raw chat
        # data doesn't linger on disk. Trust signal for skeptical users.
        export_src = getattr(args, "_export_source_path", None)
        if export_src is not None:
            _maybe_prompt_delete_export(export_src)
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
        "         tokenmin --submit-url HTTPS_URL   # hand off to a hosted endpoint (future; see ROADMAP.md)",
        "",
        "The engine lives at engine/ in this repo (Apache-2.0). A missing engine",
        "usually means a broken install — try `tokenmin doctor` to diagnose.",
    ]
    print("\n".join(msg_lines), file=sys.stderr)
    return 0


# --- terminal rendering ----------------------------------------------------

# ANSI codes. Disabled automatically when stdout isn't a tty or NO_COLOR is set.
_CONTROL_CHARS_RE = None  # lazy-compiled


def _strip_ctl(s: str) -> str:
    """Remove ANSI escape sequences and other control characters from a display
    string. Defense against adversarial filenames / project dirs / external
    inputs that could inject screen-clear, title-set, or fake-prompt sequences
    into our renderers.

    Keeps tab + newline (those are legitimate display characters); strips all
    other C0/C1 controls and CSI/OSC escape sequences.
    """
    global _CONTROL_CHARS_RE
    if _CONTROL_CHARS_RE is None:
        import re as _re
        # ANSI CSI: ESC [ ... letter
        # ANSI OSC: ESC ] ... BEL or ESC \\
        # Other ESC sequences: ESC <anything-single-char>
        # C0 control chars except \t and \n
        # C1 control chars (0x80-0x9F)
        _CONTROL_CHARS_RE = _re.compile(
            r"\x1B\[[0-?]*[ -/]*[@-~]"           # CSI
            r"|\x1B\][^\x07\x1B]*(?:\x07|\x1B\\)"  # OSC
            r"|\x1B[@-Z\\-_]"                    # other ESC sequences
            r"|[\x00-\x08\x0B-\x1F\x7F-\x9F]"    # C0 (sans \t \n) + DEL + C1
        )
    if not isinstance(s, str):
        return s
    return _CONTROL_CHARS_RE.sub("", s)


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
    """Returns (pill_text, color_code_attr, label) for a finding's $/mo.

    Pills are stars (★) — not dollar signs — so the visual tier reads as
    severity, not money. Pro/Max users were getting confused by `$$$$` because
    it looks like a price even though it's just "highest tier" (scanner#5).
    """
    if savings >= 500: return ("★★★★", "RED", "critical")
    if savings >= 100: return ("★★★ ", "YELLOW", "high")
    if savings >= 25:  return ("★★  ", "CYAN", "medium")
    return ("★   ", "GRAY", "low")


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
    # v0.5 detectors
    "low_cache_hit_ratio": "Anthropic's Claude Code engineering team: 'a few percentage points of cache miss rate can dramatically affect cost and latency.' Cache reads bill at 0.1x input rate. Target >90% on repeated workloads.",
    "multi_model_sessions": "Each /model switch reprocesses the full session history at full input rate. opusplan plan-mode toggles count too. Pick one model per session; /clear to start fresh.",
    "no_output_style": "Claude calibrates response length to perceived complexity. `outputStyle: \"concise\"` in settings.json cuts that — one line, claimed 40-65% output reduction on chatty workloads.",
    "mcp_overflow_no_tool_search": "Anthropic measured 191,300 -> 122,800 token context recovery with `ENABLE_TOOL_SEARCH=auto`. Tools still callable; definitions load lazily instead of dumping into the system prompt.",
    "high_context_per_turn": "Sonnet 4.5 accuracy degrades past 256K and collapses to ~18% past 500K. Bigger context isn't free — Claude can't fully use it past those thresholds.",
}


def _fmt_quota_pct(savings_usd: float, monthly_api: float) -> str:
    """Pro/Max framing: render savings as % of API-equivalent monthly cost."""
    if monthly_api <= 0:
        return "~? quota"
    pct = min(95.0, 100.0 * savings_usd / monthly_api)
    return f"~{pct:.0f}% quota"


def _render_terminal(result: dict) -> None:
    """Print the inline headline card + ranked findings card. The 'magic moment'.

    Plan-aware: dollar framing for API/unknown users, quota-stretch % for
    Pro/Max (rec E from the cost-framing redesign — flat-fee users don't have
    a $-denominated bill to reduce, so $/mo is misleading on those plans).
    """
    c = _C(_ansi_supported())
    snap = result.get("snapshot", {})
    findings = result.get("findings") or []
    total_save = result.get("total_savings_usd_per_month", 0.0)
    total_eff = result.get("total_hours_to_implement", 0.0)
    cost = snap.get("total_cost_usd", 0.0)
    monthly_api = snap.get("monthly_api_equivalent_cost_usd", 0.0)
    sessions = snap.get("sessions", 0)
    days = snap.get("window_days", 0)
    models = snap.get("models") or []
    models_line = "  ·  ".join(f"{m['name']} {round(m['share']*100)}%" for m in models[:3]) or "no model data"
    plan = result.get("billing_plan", "unknown")
    subscription = plan in ("pro", "max")

    line = "─" * 72

    # Detect export-mode: ConfigSnapshot is at defaults (no global settings).
    cfg = snap.get("config") or {}
    is_export_mode = (
        not cfg.get("has_global_claude_md")
        and cfg.get("global_hook_count", 0) == 0
        and cfg.get("projects_total", 0) == 0
        and cost == 0  # exports don't carry token-cost data
    )

    # Header card.
    print()
    print(f"  {c.BOLD}{c.MAGENTA}Tokenmin{c.RESET}  Claude usage audit")
    print(f"  {c.GRAY}{line}{c.RESET}")
    print(f"  scanned {c.BOLD}{sessions}{c.RESET} sessions over {days} days")
    if subscription:
        # Don't show a dollar number — Pro/Max users pay flat. Show plan instead.
        print(f"  plan: {c.BOLD}{plan}{c.RESET} {c.DIM}(flat-fee; savings reported as quota stretch){c.RESET}")
    else:
        plan_tag = f" {c.DIM}(plan: {plan}){c.RESET}" if plan != "unknown" else (
            f" {c.DIM}— set with `tokenmin plan api|pro|max`{c.RESET}"
        )
        print(f"  API-equivalent cost (window): {c.BOLD}{_fmt_money(cost)}{c.RESET}{plan_tag}")
    print(f"  model mix: {c.DIM}{models_line}{c.RESET}")
    if is_export_mode:
        print(f"  {c.YELLOW}note:{c.RESET} {c.DIM}export-mode analysis. The export doesn't carry token counts,{c.RESET}")
        print(f"        {c.DIM}local config (CLAUDE.md / hooks / MCP), or tool calls. For the{c.RESET}")
        print(f"        {c.DIM}full picture, install Claude Code and rerun.{c.RESET}")
    print(f"  {c.GRAY}{line}{c.RESET}")
    print()

    if not findings:
        print(f"  {c.GREEN}✓{c.RESET} no findings — your setup looks clean")
        if sessions < 5:
            print(f"  {c.YELLOW}note: only {sessions} session(s) in window; rerun after more use{c.RESET}")
        _print_next_steps(c, [])
        return

    # v0.12.4 (scanner#4): split into primary (worth surfacing) and low-impact
    # (drop to a one-line footer + `tokenmin show low-impact`). The engine
    # already tagged each finding with low_impact = True/False per plan.
    primary = [f for f in findings if not f.get("low_impact", False)]
    low_impact = [f for f in findings if f.get("low_impact", False)]

    # If filtering would leave nothing, keep the highest finding so the user
    # still sees SOMETHING actionable.
    if not primary and findings:
        primary = [findings[0]]
        low_impact = findings[1:]

    # Headline — plan-aware. Counts include both buckets so the user knows
    # the engine isn't broken when 7 findings collapse to 3 in the display.
    if subscription:
        total_unit = _fmt_quota_pct(total_save, monthly_api)
        print(f"  {c.BOLD}{c.YELLOW}Headline{c.RESET}  {c.BOLD}{total_unit}{c.RESET} stretch across {len(findings)} fix(es), ~{total_eff:.1f} hrs total")
    else:
        print(f"  {c.BOLD}{c.YELLOW}Headline{c.RESET}  ~{c.BOLD}{_fmt_money(total_save)}/mo{c.RESET} recoverable across {len(findings)} fix(es), ~{total_eff:.1f} hrs total")
    print()

    max_save = max((f["savings_usd_per_month"] for f in primary), default=1.0) or 1.0

    for i, f in enumerate(primary, 1):
        save = f["savings_usd_per_month"]
        pill, color_attr, _ = _severity(save)
        pill_color = getattr(c, color_attr)
        rel = save / max_save if max_save > 0 else 0
        pillar = _PILLAR_LABELS.get(f.get("pillar", ""), "")
        conf = int(f.get("confidence", 0) * 100)
        hrs = f.get("hours_to_implement", 0.0)

        # Strip control chars — ANSI injection defense via finding titles.
        title = _strip_ctl(f["title"])
        evidence = _strip_ctl(f.get("evidence", ""))
        finding_id = _strip_ctl(f["id"])

        if subscription:
            save_unit = _fmt_quota_pct(save, monthly_api)
        else:
            save_unit = f"{_fmt_money(save)}/mo"

        print(f"  {c.BOLD}{i}.{c.RESET} {c.BOLD}{title}{c.RESET}")
        print(f"     {pill_color}{pill}{c.RESET}  {_bar(rel)}  {c.BOLD}{save_unit}{c.RESET}  {c.DIM}{hrs:.1f} hrs · conf {conf}% · {pillar}{c.RESET}")
        print(f"     {c.DIM}evidence:{c.RESET} {evidence}")
        print(f"     {c.CYAN}→{c.RESET} {c.DIM}tokenmin show {finding_id}{c.RESET}")
        print()

    if low_impact:
        print(f"  {c.DIM}+ {len(low_impact)} low-impact finding(s) hidden — "
              f"{c.RESET}{c.CYAN}tokenmin show low-impact{c.RESET}{c.DIM} to see{c.RESET}")
        print()

    _print_next_steps(c, primary)


def _print_next_steps(c: "_C", findings: list) -> None:
    print(f"  {c.GRAY}{'─' * 72}{c.RESET}")
    print(f"  next steps:")
    if findings:
        print(f"    {c.BOLD}tokenmin show <id>{c.RESET}    drill into one finding")
    print(f"    {c.BOLD}tokenmin --out report.md{c.RESET}  write the full markdown report")
    print(f"    {c.BOLD}tokenmin help{c.RESET}             30-second walkthrough")
    print(f"    {c.GRAY}guide: https://tokenmin.ai/guides/claude-token-optimization{c.RESET}")
    print()


def _render_show(finding_id: str) -> int:
    """Drill-down into one finding. Reads last_run.json.

    Special id `low-impact` lists all findings the engine flagged as below
    the per-plan impact threshold (rec from scanner#4 — those don't pollute
    the main audit but the user can still inspect them on demand).

    Plan-aware: subscription users see `~Y% quota` instead of `$X/mo`
    (scanner#5 — show was leaking dollars even after v0.12.3).
    """
    c = _C(_ansi_supported())
    last = _load_last_run()
    if not last:
        print("tokenmin show: no recent run found.", file=sys.stderr)
        print("  run `tokenmin` first to produce findings, then `tokenmin show <id>`.", file=sys.stderr)
        return 2
    findings = last.get("findings") or []
    plan = last.get("billing_plan", "unknown")
    snap_info = last.get("snapshot") or {}
    monthly_api = snap_info.get("monthly_api_equivalent_cost_usd", 0.0)
    subscription = plan in ("pro", "max")

    # Special: list all low-impact findings.
    if finding_id == "low-impact":
        low = [f for f in findings if f.get("low_impact", False)]
        if not low:
            print(f"  {c.GREEN}✓{c.RESET} no low-impact findings in the last run", file=sys.stderr)
            return 0
        print(file=sys.stderr)
        print(f"  {c.BOLD}{c.MAGENTA}Low-impact findings{c.RESET} ({len(low)})", file=sys.stderr)
        print(f"  {c.DIM}hidden from the main audit because each is below the per-plan threshold.{c.RESET}", file=sys.stderr)
        print(file=sys.stderr)
        for f in low:
            save = f["savings_usd_per_month"]
            unit = _fmt_quota_pct(save, monthly_api) if subscription else f"{_fmt_money(save)}/mo"
            print(f"  - {c.BOLD}{_strip_ctl(f['id'])}{c.RESET}  {c.DIM}{unit} · {_strip_ctl(f['title'])}{c.RESET}", file=sys.stderr)
        print(file=sys.stderr)
        print(f"  drill into one: {c.CYAN}tokenmin show <id>{c.RESET}", file=sys.stderr)
        return 0

    found = next((f for f in findings if f["id"] == finding_id), None)
    if not found:
        print(f"tokenmin show: no finding with id '{finding_id}' in last run.", file=sys.stderr)
        if findings:
            print("  available findings:", file=sys.stderr)
            for f in findings:
                tag = " (low-impact)" if f.get("low_impact") else ""
                print(f"    {f['id']}{tag}", file=sys.stderr)
            print(f"    low-impact  (list all hidden findings)", file=sys.stderr)
        return 2

    save = found["savings_usd_per_month"]
    pill, color_attr, label = _severity(save)
    pill_color = getattr(c, color_attr)
    pillar = _PILLAR_LABELS.get(found.get("pillar", ""), "hygiene")
    conf = int(found.get("confidence", 0) * 100)
    hrs = found.get("hours_to_implement", 0.0)

    title = _strip_ctl(found["title"])
    fid = _strip_ctl(found["id"])
    evidence = _strip_ctl(found.get("evidence", ""))
    how_to_fix = _strip_ctl(found.get("how_to_fix", "") or "")

    # Plan-aware impact line — no dollar leak for Pro/Max.
    if subscription:
        impact_line = f"{c.BOLD}{_fmt_quota_pct(save, monthly_api)}{c.RESET} stretch on your flat-fee plan"
    else:
        impact_line = f"{c.BOLD}{_fmt_money(save)}/mo{c.RESET} estimated savings (at API rates)"

    print()
    print(f"  {c.BOLD}{c.MAGENTA}{title}{c.RESET}")
    print(f"  {c.GRAY}finding id: {fid}{c.RESET}")
    print()
    print(f"  Severity   {pill_color}{pill}{c.RESET} ({label})")
    print(f"  Impact     {impact_line}")
    print(f"  Effort     {hrs:.1f} hrs to implement")
    print(f"  Pillar     {pillar}")
    print(f"  Confidence {conf}%")
    print()
    print(f"  {c.BOLD}Evidence{c.RESET}")
    print(f"  {evidence}")
    anchor = _ANCHORS.get(found["id"])
    if anchor:
        print()
        print(f"  {c.BOLD}Why this matters{c.RESET}")
        print(f"  {c.DIM}{anchor}{c.RESET}")
    print()
    print(f"  {c.BOLD}How to fix{c.RESET}")
    for line in how_to_fix.splitlines():
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
    print(f"  {c.BOLD}Live dashboard{c.RESET}")
    print(f"    {c.CYAN}tokenmin watch{c.RESET}                real-time spend + cache hit + sparkline (Ctrl-C to exit)")
    print(f"    {c.CYAN}tokenmin watch --alert 5{c.RESET}      beep when active session crosses $5")
    print()
    print(f"  {c.BOLD}claude.ai / Claude Desktop (export-based)")
    print(f"    {c.CYAN}tokenmin help-export{c.RESET}                step-by-step + browser deep-link")
    print(f"    {c.CYAN}tokenmin --source export --from latest{c.RESET}  auto-pick newest ~/Downloads export")
    print(f"    {c.CYAN}tokenmin --source export --watch-downloads{c.RESET}  wait for export to arrive")
    print(f"    {c.CYAN}tokenmin demo{c.RESET}                       see a sample report without exporting")
    print()
    print(f"  {c.BOLD}Variants{c.RESET}")
    print(f"    {c.CYAN}tokenmin --source export --from FILE{c.RESET}    audit a specific export file")
    print(f"    {c.CYAN}tokenmin --out report.md{c.RESET}                write full markdown report")
    print(f"    {c.CYAN}tokenmin --snapshot snap.json{c.RESET}           inspect anonymized payload")
    print(f"    {c.CYAN}tokenmin --submit-url URL{c.RESET}               send to hosted engine (HTTPS only)")
    print()
    print(f"  {c.BOLD}Maintenance{c.RESET}")
    print(f"    {c.CYAN}tokenmin --version{c.RESET}            what you're running")
    print(f"    {c.CYAN}tokenmin doctor{c.RESET}               self-diagnose")
    print(f"    {c.CYAN}tokenmin selftest{c.RESET}             run the bundled tests")
    print(f"    {c.CYAN}tokenmin telemetry status{c.RESET}     view telemetry state (off / on / dry-run)")
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
        # v0.12.4: persist billing_plan so `tokenmin show <id>` can format
        # impact in the right unit (quota stretch for Pro/Max, dollars for
        # API). Without this, show always defaults to "unknown" → dollars
        # leak even after the v0.12.3 main-audit fix.
        "billing_plan": result.get("billing_plan", "unknown"),
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


# --- tokenmin watch (live dashboard) ---------------------------------------

# Pricing now lives in engine/pricing.json — see engine/pricing.py for the
# loader. Keeping a single _watch_price wrapper so the call sites elsewhere in
# this file don't need to know whether the engine is bundled.
_engine_dir = Path(__file__).resolve().parent.parent.parent / "engine"
if str(_engine_dir) not in sys.path:
    sys.path.insert(0, str(_engine_dir))
try:
    from pricing import price_for as _watch_price  # type: ignore
except ImportError:
    # Scanner-only install (no engine) — fall back to last-known rates.
    _WATCH_PRICING_FALLBACK = {
        "opus":   (15.00, 75.00, 18.75, 1.50),
        "sonnet": ( 3.00, 15.00,  3.75, 0.30),
        "haiku":  ( 0.80,  4.00,  1.00, 0.08),
    }
    def _watch_price(model):
        if not model:
            return _WATCH_PRICING_FALLBACK["sonnet"]
        m = model.lower()
        for key, prices in _WATCH_PRICING_FALLBACK.items():
            if key in m:
                return prices
        return _WATCH_PRICING_FALLBACK["sonnet"]


def _active_sessions(claude_home: Path, max_age_sec: float) -> list[Path]:
    """Return jsonl session files modified within max_age_sec, newest first."""
    proj_dir = claude_home / "projects"
    if not proj_dir.is_dir():
        return []
    now = time.time()
    found: list[tuple[float, Path]] = []
    for p in proj_dir.rglob("*.jsonl"):
        try:
            mt = p.stat().st_mtime
            if now - mt <= max_age_sec:
                found.append((mt, p))
        except OSError:
            continue
    found.sort(reverse=True)
    return [p for _, p in found]


def _parse_session_live(path: Path) -> dict:
    """Cheap parse of one jsonl session. Returns aggregated metrics for the dashboard.

    Defensive about schema (same as analyzer.py). Stops on first read error
    rather than raising — the file might be mid-write."""
    from collections import Counter as _Counter
    stats = {
        "session_id": path.stem,
        "project": path.parent.name,
        "started_at": None,
        "last_at": None,
        "user_turns": 0,
        "assistant_turns": 0,
        "tool_calls": _Counter(),
        "models": _Counter(),
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "est_cost_usd": 0.0,
        "size_bytes": 0,
    }
    # Defense against adversarial JSONL: skip files > 50 MiB outright and
    # cap per-line reads at 1 MiB via readline(maxsize). Without this, a
    # single multi-GB line would OOM Python during the `for line in f` read.
    _MAX_FILE = 50 * 1024 * 1024
    _MAX_LINE = 1024 * 1024
    try:
        stats["size_bytes"] = path.stat().st_size
        if stats["size_bytes"] > _MAX_FILE:
            return stats  # file too large to safely live-parse
        with path.open("r", encoding="utf-8", errors="replace") as f:
            while True:
                line = f.readline(_MAX_LINE)
                if not line:
                    break  # EOF
                # If we hit the size cap without a newline, discard the rest
                # of this "line" so the next readline starts fresh.
                if len(line) == _MAX_LINE and not line.endswith("\n"):
                    # Skip ahead to the next newline (bounded read).
                    while True:
                        chunk = f.readline(_MAX_LINE)
                        if not chunk or chunk.endswith("\n"):
                            break
                    continue
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                ts = event.get("timestamp")
                if isinstance(ts, str):
                    try:
                        t = datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
                        if stats["started_at"] is None:
                            stats["started_at"] = t
                        stats["last_at"] = t
                    except ValueError:
                        pass
                role = event.get("type") or event.get("role")
                inner = event.get("message") or {}
                role = role or inner.get("role")
                if role == "user":
                    stats["user_turns"] += 1
                elif role == "assistant":
                    stats["assistant_turns"] += 1
                    usage = event.get("usage") or inner.get("usage") or {}
                    model = event.get("model") or inner.get("model")
                    it = int(usage.get("input_tokens", 0) or 0)
                    ot = int(usage.get("output_tokens", 0) or 0)
                    cw = int(usage.get("cache_creation_input_tokens", 0) or 0)
                    cr = int(usage.get("cache_read_input_tokens", 0) or 0)
                    stats["input_tokens"] += it
                    stats["output_tokens"] += ot
                    stats["cache_write_tokens"] += cw
                    stats["cache_read_tokens"] += cr
                    if model:
                        stats["models"][model] += 1
                        pi, po, pcw, pcr = _watch_price(model)
                        stats["est_cost_usd"] += (it * pi + ot * po + cw * pcw + cr * pcr) / 1_000_000
                    # walk content for tool_use
                    content = inner.get("content") or event.get("content") or []
                    if isinstance(content, list):
                        for b in content:
                            if isinstance(b, dict) and b.get("type") == "tool_use":
                                stats["tool_calls"][b.get("name", "?")] += 1
    except OSError:
        pass
    return stats


def _fmt_tok(n: int) -> str:
    if n >= 1_000_000:
        return f"{n/1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n/1_000:.0f}K"
    return str(n)


def _fmt_duration(start: float | None, end: float | None) -> str:
    if not start or not end:
        return "—"
    s = max(0, int(end - start))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s//60}m{s%60}s"
    return f"{s//3600}h{(s%3600)//60}m"


_SPARKLINE_CHARS = "▁▂▃▄▅▆▇█"


def _sparkline(values: list[float]) -> str:
    if not values:
        return ""
    vmax = max(values) or 1.0
    out = []
    for v in values:
        if v <= 0:
            out.append("·")
        else:
            idx = min(len(_SPARKLINE_CHARS) - 1, int((v / vmax) * (len(_SPARKLINE_CHARS) - 1)))
            out.append(_SPARKLINE_CHARS[idx])
    return "".join(out)


def _watch(args: list[str]) -> int:
    """Live dashboard. Polls ~/.claude every poll_interval seconds and refreshes."""
    import argparse as _ap
    from collections import deque
    sp = _ap.ArgumentParser(prog="tokenmin watch", description="Live token-spend dashboard.")
    sp.add_argument("--claude-home", default=str(Path.home() / ".claude"))
    sp.add_argument("--interval", type=float, default=2.0, help="Poll interval in seconds (default 2)")
    sp.add_argument(
        "--max-idle-min",
        type=float,
        default=15.0,
        help="Sessions idle > this many minutes drop off the dashboard (default 15)",
    )
    sp.add_argument(
        "--alert",
        type=float,
        default=None,
        help="Beep when active session cost crosses this $ threshold",
    )
    a = sp.parse_args(args)

    home = Path(a.claude_home).expanduser()
    if not home.exists():
        print(f"tokenmin watch: {home} does not exist.", file=sys.stderr)
        return 2

    c = _C(_ansi_supported())
    # Disable line-buffering on stdout so the redraws don't flicker awkwardly.
    sys.stdout.reconfigure(line_buffering=False) if hasattr(sys.stdout, "reconfigure") else None

    last_run = _load_last_run() or {}
    top_findings = (last_run.get("findings") or [])[:3]

    # Sparkline history: (input+output) tokens per poll, kept as a 30-wide deque
    history: deque[float] = deque(maxlen=30)
    prev_total = 0.0
    alerted = False

    # Hide cursor + ensure we restore on exit.
    print("\033[?25l", end="", flush=True)
    try:
        while True:
            sessions = _active_sessions(home, a.max_idle_min * 60)
            now = time.time()

            # Clear screen + home cursor.
            print("\033[2J\033[H", end="")

            # Header.
            print(f"{c.BOLD}{c.MAGENTA}Tokenmin watch{c.RESET}  {c.GRAY}refresh {a.interval:.0f}s · Ctrl-C to exit{c.RESET}")
            print(f"{c.GRAY}{'─'*72}{c.RESET}")

            if not sessions:
                print()
                print(f"  {c.DIM}no active Claude Code session in the last {int(a.max_idle_min)} min{c.RESET}")
                print(f"  {c.DIM}start a Claude Code session and this dashboard will populate{c.RESET}")
            else:
                active = _parse_session_live(sessions[0])
                others = len(sessions) - 1

                # Track delta + sparkline
                current_total = active["input_tokens"] + active["output_tokens"]
                delta = max(0.0, current_total - prev_total)
                history.append(delta)
                prev_total = current_total

                # Alert
                if a.alert is not None and not alerted and active["est_cost_usd"] >= a.alert:
                    print("\007", end="", flush=True)
                    alerted = True

                # Session card.
                started_iso = (
                    datetime.fromtimestamp(active["started_at"], tz=timezone.utc).strftime("%H:%M UTC")
                    if active["started_at"] else "—"
                )
                last_iso = (
                    datetime.fromtimestamp(active["last_at"], tz=timezone.utc).strftime("%H:%M:%S UTC")
                    if active["last_at"] else "—"
                )
                duration = _fmt_duration(active["started_at"], active["last_at"])

                print()
                print(f"  {c.BOLD}Active session{c.RESET}  {c.DIM}{_strip_ctl(active['project'])[:50]}{c.RESET}")
                print(f"    started   {started_iso}    duration {duration}    last activity {last_iso}")
                print()

                # Token + cost table
                in_tok = active["input_tokens"]
                out_tok = active["output_tokens"]
                cr = active["cache_read_tokens"]
                cw = active["cache_write_tokens"]
                cost = active["est_cost_usd"]

                cache_total = cr + cw + in_tok
                cache_hit = (cr / cache_total) if cache_total > 0 else 0
                cache_bar = _bar(cache_hit, width=20)

                print(f"  {c.BOLD}Spend this session{c.RESET}  {c.BOLD}{_fmt_money(cost)}{c.RESET}")
                print(f"    input {c.BOLD}{_fmt_tok(in_tok)}{c.RESET}   output {c.BOLD}{_fmt_tok(out_tok)}{c.RESET}   cache-read {c.BOLD}{_fmt_tok(cr)}{c.RESET}   cache-write {c.BOLD}{_fmt_tok(cw)}{c.RESET}")
                cache_color = c.GREEN if cache_hit >= 0.5 else c.YELLOW if cache_hit >= 0.2 else c.RED
                print(f"    cache hit  {cache_color}{cache_bar}{c.RESET}  {int(cache_hit*100)}%   {c.DIM}(Anthropic target >90% for repeated workloads){c.RESET}")
                print()

                # Models in session
                if active["models"]:
                    total_calls = sum(active["models"].values())
                    model_line = "  ·  ".join(
                        f"{_strip_ctl(m.split('-')[1].title() if '-' in m else m)} {round(100*n/total_calls)}%"
                        for m, n in active["models"].most_common(3)
                    )
                    print(f"  {c.BOLD}Models{c.RESET}        {model_line}")

                # Tools
                if active["tool_calls"]:
                    total_t = sum(active["tool_calls"].values())
                    tool_line = "  ·  ".join(
                        f"{_strip_ctl(name)} {round(100*n/total_t)}%"
                        for name, n in active["tool_calls"].most_common(5)
                    )
                    print(f"  {c.BOLD}Tools{c.RESET}         {tool_line}")

                print(f"  {c.BOLD}Turns{c.RESET}         user {active['user_turns']}  ·  assistant {active['assistant_turns']}")
                print()

                # Sparkline of recent token deltas
                if history:
                    spark = _sparkline(list(history))
                    print(f"  {c.BOLD}Token rate{c.RESET}    {c.CYAN}{spark}{c.RESET}   {c.DIM}last {len(history)} polls{c.RESET}")
                    print()

                if others:
                    print(f"  {c.DIM}+ {others} other session(s) active in the last {int(a.max_idle_min)} min{c.RESET}")
                    print()

            # Top findings overlay from last_run.json
            if top_findings:
                print(f"{c.GRAY}{'─'*72}{c.RESET}")
                print(f"  {c.BOLD}Top findings from last `tokenmin` run{c.RESET}")
                for i, f in enumerate(top_findings, 1):
                    save = f["savings_usd_per_month"]
                    pill, color_attr, _ = _severity(save)
                    pill_color = getattr(c, color_attr)
                    print(f"    {i}. {pill_color}{pill:<4}{c.RESET} {_fmt_money(save)}/mo  {_strip_ctl(f['title'])[:60]}")
                print()
            else:
                print(f"  {c.DIM}(run `tokenmin` once to populate top findings here){c.RESET}")
                print()

            time.sleep(a.interval)
    except KeyboardInterrupt:
        pass
    finally:
        # Restore cursor + give a clean prompt.
        print("\033[?25h", end="", flush=True)
        print()
    return 0


# --- demo / sample report --------------------------------------------------

def _demo() -> int:
    """Run a full tokenmin scan against a baked-in sample export. Shows the
    user what a report looks like without requiring any real export from
    claude.ai or Claude Desktop. No collection, no network."""
    c = _C(_ansi_supported())
    sample = Path(__file__).resolve().parent / "demo" / "conversations.json"
    if not sample.exists():
        print(f"tokenmin demo: sample fixture missing at {sample}", file=sys.stderr)
        return 2
    print(f"  {c.DIM}▶ running tokenmin against the bundled demo conversations...{c.RESET}", file=sys.stderr)
    print(f"  {c.DIM}  (no collection, no network — this is a sample dataset){c.RESET}", file=sys.stderr)
    print(file=sys.stderr)
    snap = collect_from_export(sample, days=90)
    snapshot = _label_scrub_pass(_dataclass_to_dict(snap))
    snapshot = scrub_value(snapshot)
    structured_engine = _local_engine_structured()
    if structured_engine is None:
        print("tokenmin demo: engine not importable — can't show a report.", file=sys.stderr)
        print("  (try `tokenmin doctor` to diagnose; the engine ships in this repo at engine/.)", file=sys.stderr)
        return 0
    result = structured_engine(snapshot)
    _render_terminal(result)
    print(file=sys.stderr)
    print(f"  {c.DIM}^ this is the sample. for your real data:{c.RESET}", file=sys.stderr)
    print(f"  {c.CYAN}tokenmin{c.RESET}                                    {c.DIM}# Claude Code{c.RESET}", file=sys.stderr)
    print(f"  {c.CYAN}tokenmin --source export --from <export.zip>{c.RESET}  {c.DIM}# claude.ai or Desktop{c.RESET}", file=sys.stderr)
    print(f"  {c.CYAN}tokenmin help-export{c.RESET}                        {c.DIM}# how to export your data{c.RESET}", file=sys.stderr)
    return 0


def _help_export() -> int:
    """Step-by-step instructions for exporting from claude.ai or Claude Desktop,
    plus an optional deep-link that opens the right page in the user's browser."""
    import platform as _plat
    c = _C(_ansi_supported())
    EXPORT_URL = "https://claude.ai/settings/data-privacy-controls"
    print()
    print(f"  {c.BOLD}{c.MAGENTA}Exporting your Claude data{c.RESET}")
    print()
    print(f"  Both claude.ai and Claude Desktop use the same export flow.")
    print()
    print(f"  {c.BOLD}1.{c.RESET}  Sign in to claude.ai")
    print(f"  {c.BOLD}2.{c.RESET}  Open Settings -> Privacy -> Data controls")
    print(f"      direct link: {c.BLUE}{EXPORT_URL}{c.RESET}")
    print(f"  {c.BOLD}3.{c.RESET}  Click 'Export data'")
    print(f"  {c.BOLD}4.{c.RESET}  Anthropic emails the export zip when ready (usually minutes)")
    print(f"  {c.BOLD}5.{c.RESET}  Download the zip (typically to ~/Downloads/)")
    print(f"  {c.BOLD}6.{c.RESET}  Run:")
    print(f"        {c.CYAN}tokenmin --source export --from ~/Downloads/data-export-*.zip{c.RESET}")
    print(f"      or, to auto-pick the most recent Anthropic export:")
    print(f"        {c.CYAN}tokenmin --source export --from latest{c.RESET}")
    print()
    print(f"  {c.BOLD}Watch-mode (run when the export arrives):{c.RESET}")
    print(f"    {c.CYAN}tokenmin --source export --watch-downloads{c.RESET}")
    print(f"    Polls ~/Downloads/ for new Anthropic export zips and runs")
    print(f"    automatically when one shows up. Ctrl-C exits.")
    print()
    # On macOS, offer to open the export page.
    if _plat.system() == "Darwin" and sys.stdin.isatty() and sys.stderr.isatty():
        try:
            print(f"  {c.DIM}Open the export page in your browser now? [y/N] {c.RESET}", end="")
            sys.stderr.flush()
            ans = input().strip().lower()
            if ans in ("y", "yes"):
                import subprocess
                subprocess.run(["open", EXPORT_URL], check=False)
                print(f"  {c.GREEN}✓{c.RESET} opened in browser")
        except (EOFError, KeyboardInterrupt):
            pass
    print()
    return 0


# --- --from latest / --watch-downloads helpers -----------------------------

def _is_anthropic_export_name(name: str) -> bool:
    """Strict-but-defensive: only file names that look like Anthropic chat
    exports, NOT generic 'export' files from other services (LinkedIn,
    Twitter, etc.).

    Anthropic's export uses patterns like:
      data-export-20260520T143000.zip
      conversations.zip
      claude-export-<uuid>.zip
      anthropic-export-<...>.zip
    """
    n = name.lower()
    return (
        n == "conversations.zip"
        or n.startswith("data-export-")
        or n.startswith("claude-export")
        or n.startswith("anthropic-export")
    )


def _find_latest_export(downloads_dir: Path | None = None) -> Path | None:
    """Find the most recently modified Anthropic export zip in ~/Downloads/."""
    if downloads_dir is None:
        downloads_dir = Path.home() / "Downloads"
    if not downloads_dir.is_dir():
        return None
    candidates: list[tuple[float, Path]] = []
    for p in downloads_dir.glob("*.zip"):
        if _is_anthropic_export_name(p.name):
            try:
                candidates.append((p.stat().st_mtime, p))
            except OSError:
                continue
    if not candidates:
        return None
    candidates.sort(reverse=True)
    return candidates[0][1]


def _watch_downloads_for_export(out_path: str | None, force: bool) -> int:
    """Poll ~/Downloads/ until a new Anthropic export zip arrives, then run.

    Idempotent — if an export already exists when started, runs against the
    newest immediately. Otherwise polls until one appears.
    """
    c = _C(_ansi_supported())
    downloads = Path.home() / "Downloads"
    seen_at_start = {}
    if downloads.is_dir():
        for p in downloads.glob("*.zip"):
            try:
                seen_at_start[p] = p.stat().st_mtime
            except OSError:
                continue
    initial = _find_latest_export(downloads)
    if initial:
        print(f"  {c.GREEN}✓{c.RESET} found existing export: {initial.name}", file=sys.stderr)
        print(f"  {c.DIM}(running against it now; rerun with --watch-downloads for the next one){c.RESET}", file=sys.stderr)
        return _run_export(initial, out_path, force)
    print(f"  {c.DIM}▶ watching {downloads} for a new Anthropic export zip...{c.RESET}", file=sys.stderr)
    print(f"  {c.DIM}  (Settings -> Privacy -> Export data on claude.ai. Ctrl-C to exit.){c.RESET}", file=sys.stderr)
    try:
        while True:
            time.sleep(3)
            if not downloads.is_dir():
                continue
            for p in downloads.glob("*.zip"):
                try:
                    mt = p.stat().st_mtime
                except OSError:
                    continue
                if _is_anthropic_export_name(p.name) and (p not in seen_at_start or mt > seen_at_start[p]):
                    print(f"  {c.GREEN}✓{c.RESET} export arrived: {p.name}", file=sys.stderr)
                    return _run_export(p, out_path, force)
                seen_at_start[p] = mt
    except KeyboardInterrupt:
        print(f"\n  {c.DIM}stopped watching.{c.RESET}", file=sys.stderr)
        return 0


def _run_export(export_path: Path, out_path: str | None, force: bool) -> int:
    """Inline export run — shared between --from <path> and --watch-downloads paths."""
    snap = collect_from_export(export_path, days=90)
    snapshot = _label_scrub_pass(_dataclass_to_dict(snap))
    snapshot = scrub_value(snapshot)
    structured_engine = _local_engine_structured()
    if structured_engine is None:
        print("tokenmin: no engine bundled. Snapshot ready; pass --snapshot PATH or --submit-url to use it.", file=sys.stderr)
        return 0
    result = structured_engine(snapshot)
    _save_last_run(result)
    if out_path:
        _emit_report(result.get("report_md", ""), out_path)
    else:
        _render_terminal(result)
    _maybe_prompt_delete_export(export_path)
    return 0


def _maybe_prompt_delete_export(export_path: Path) -> None:
    """After a successful export run, offer to delete the source zip so the
    raw chat data doesn't linger on disk. Trust signal."""
    if not sys.stdin.isatty() or not sys.stderr.isatty():
        return
    c = _C(_ansi_supported())
    try:
        print()
        print(f"  {c.DIM}delete the export file {export_path.name} now? "
              f"(the snapshot kept by tokenmin is anonymized; the export is the raw source) [y/N] {c.RESET}",
              end="", file=sys.stderr)
        sys.stderr.flush()
        ans = input().strip().lower()
        if ans in ("y", "yes"):
            export_path.unlink()
            print(f"  {c.GREEN}✓{c.RESET} deleted {export_path}", file=sys.stderr)
    except (EOFError, KeyboardInterrupt, OSError):
        pass


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


# --- settings + telemetry --------------------------------------------------
#
# Telemetry is a strictly-bounded, fixed-fields signal to help improve the
# detector ranking and surface real install / crash bugs. The full per-field
# dictionary is enumerated in `_TELEMETRY_FIELDS_DOC` below and mirrored in
# SECURITY.md.
#
# Posture:
#   - F&F invitees:    default ON (the per-invite installer sets it at install)
#   - Public scanner:  default OFF, first-run consent flow asks once
#   - TOKENMIN_NO_TELEMETRY=1 always wins, regardless of settings
#   - `tokenmin telemetry off` disables permanently
#   - `tokenmin telemetry dry-run` prints what would be sent without sending
#   - Endpoint failures are silent (telemetry is non-critical)
#
# Privacy invariants:
#   - Never send: snapshot, file paths, project names, raw error messages,
#     user-agent / IP (server-side discards), email, model-specific identifiers
#   - install_id is HMAC-derived from the per-install salt + a separate
#     "install-id-v1" tag, NOT the salt itself — different value space so the
#     anonymization hash and the install_id never collide.

_SETTINGS_PATH = Path.home() / ".tokenmin" / "settings.json"


def _load_settings() -> dict:
    try:
        return json.loads(_SETTINGS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _save_settings(s: dict) -> None:
    _SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(s, indent=2, sort_keys=True).encode("utf-8")
    tmp = _SETTINGS_PATH.with_suffix(".tmp")
    fd = os.open(str(tmp), os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)
    os.replace(str(tmp), str(_SETTINGS_PATH))


def _telemetry_install_id() -> str:
    """Derive a stable per-install ID from the salt, in a different value
    space than the anonymization hash so the two can never collide."""
    import hmac as _hmac
    try:
        from anonymize import _INSTALL_SALT
    except Exception:
        return "unknown"
    return _hmac.new(_INSTALL_SALT, b"install-id-v1", hashlib.sha256).hexdigest()[:16]


def _telemetry_enabled() -> bool:
    if os.environ.get("TOKENMIN_NO_TELEMETRY"):
        return False
    s = _load_settings()
    return s.get("telemetry") == "on"


def _telemetry_endpoint() -> str | None:
    """Where to POST telemetry. None means 'don't send' — events are still
    formed and the dry-run flag still works, just nothing transmits."""
    s = _load_settings()
    return s.get("telemetry_endpoint")


def _bucket(value: float, edges: list[tuple[float, str]]) -> str:
    """Return the first bucket label whose upper edge `value` falls under."""
    for upper, label in edges:
        if value < upper:
            return label
    return edges[-1][1]


def _build_telemetry_event(
    *,
    subcommand: str,
    findings_fired: list[str] | None = None,
    session_count: int | None = None,
    models_used_families: dict | None = None,
    error: tuple[str, str] | None = None,
    snapshot_summary: dict | None = None,
    config_summary: dict | None = None,
) -> dict:
    """Telemetry payload — fixed shape, bucketed values only.

    Purpose split across two design goals:
      1. Rank existing detectors by population fire-rate (findings_fired).
      2. Discover NEW detectors by observing distribution shape of metrics
         that no current detector reads (metrics + setup_signature).

    Privacy: everything numeric is bucketed so an attacker with the corpus
    can't reverse a single install's exact values. Identifiers (install_id,
    top_tool) are hash- or family-typed.
    """
    import platform as _plat
    install_id = _telemetry_install_id()
    info = _version_info()
    event = {
        "schema": "tokenmin.telemetry.v1",
        "sent_at": datetime.now(timezone.utc).isoformat(),
        "install_id": install_id,
        "version": info.get("version") or "dev",
        "platform": f"{_plat.system()} {_plat.release()}",
        "python_version": f"{sys.version_info.major}.{sys.version_info.minor}",
        "subcommand": subcommand,
    }
    if findings_fired is not None:
        event["findings_fired"] = sorted(findings_fired)
    if session_count is not None:
        if session_count == 0:      event["session_count_bucket"] = "0"
        elif session_count <= 10:    event["session_count_bucket"] = "1-10"
        elif session_count <= 100:   event["session_count_bucket"] = "11-100"
        else:                        event["session_count_bucket"] = "101+"
    if models_used_families is not None:
        event["models_used_families"] = {
            k: int(v) for k, v in models_used_families.items()
        }

    # --- discovery fields ---------------------------------------------------
    # Bucketed distribution shapes, NOT raw values. The goal is to make it
    # possible to spot "there's a cluster at X% cache hit, lower than we'd
    # predict — maybe a new detector lives there."
    if snapshot_summary is not None:
        s = snapshot_summary
        metrics = {}

        # Cache hit ratio bucket (whole distribution, not just <50% which is
        # what detect_low_cache_hit_ratio looks at).
        cr = s.get("cache_read_tokens") or 0
        cw = s.get("cache_write_tokens") or 0
        it = s.get("input_tokens") or 0
        denom = cr + cw + it
        if denom > 0:
            ratio = cr / denom
            metrics["cache_hit_bucket"] = _bucket(ratio, [
                (0.20, "very-low"), (0.50, "low"), (0.80, "medium"),
                (0.95, "high"),    (1.01, "very-high"),
            ])

        # Avg tools per assistant turn (parallelism signal).
        atpt = s.get("avg_tools_per_turn")
        if atpt is not None:
            metrics["avg_tools_per_turn_bucket"] = _bucket(atpt, [
                (1.5, "sequential"), (2.5, "mostly-sequential"),
                (4.0, "moderate-parallel"), (8.0, "high-parallel"),
                (1000.0, "extreme-parallel"),
            ])

        # Top tool by share (already anonymized — mcp__* names hash via
        # _label_scrub_pass, others are public Claude Code tool names).
        top_tools = s.get("top_tools") or []
        if top_tools:
            metrics["top_tool"] = top_tools[0].get("name")

        # Cost-window bucket (USD/window) — distribution of spend intensity.
        cost = s.get("total_cost_usd") or 0
        if cost > 0:
            metrics["window_cost_bucket"] = _bucket(cost, [
                (1.0,   "trial"),     (50.0,    "light"),
                (500.0, "moderate"),  (5000.0,  "heavy"),
                (1e9,   "very-heavy"),
            ])

        # Per-turn input tokens average (context-pressure signal).
        ut = s.get("user_turns") or 0
        if ut > 0:
            avg_in_per_turn = it / ut
            metrics["avg_input_per_turn_bucket"] = _bucket(avg_in_per_turn, [
                (1_000,    "minimal"),   (10_000,  "small"),
                (50_000,   "moderate"),  (200_000, "large"),
                (1e9,      "extreme"),
            ])

        if metrics:
            event["metrics"] = metrics

    if config_summary is not None:
        # Setup signature — categorical features of the install's config.
        # Each is bucketed; the combination clusters users into setup types
        # without revealing identifiable specifics.
        cfg = config_summary
        sig = {}
        sig["has_global_claude_md"] = bool(cfg.get("has_global_claude_md"))
        cml = cfg.get("global_claude_md_lines") or 0
        sig["claude_md_size_bucket"] = _bucket(cml, [
            (1,    "absent"),  (100,  "small"),  (200,  "medium"),
            (500,  "large"),   (10_000, "xlarge"),
        ])
        sig["hooks_bucket"] = _bucket(cfg.get("global_hook_count") or 0, [
            (1, "none"), (3, "few"), (10, "some"), (10_000, "many"),
        ])
        sig["mcp_bucket"] = _bucket(cfg.get("mcp_servers") or 0, [
            (1, "none"), (3, "few"), (10, "some"), (10_000, "many"),
        ])
        sig["custom_agents_bucket"] = _bucket(cfg.get("custom_agents") or 0, [
            (1, "none"), (3, "few"), (10, "some"), (10_000, "many"),
        ])
        sig["custom_skills_bucket"] = _bucket(cfg.get("custom_skills") or 0, [
            (1, "none"), (3, "few"), (10, "some"), (10_000, "many"),
        ])
        sig["output_style_set"] = bool(cfg.get("output_style"))
        sig["enable_tool_search_set"] = bool(cfg.get("enable_tool_search"))
        event["setup_signature"] = sig

    if error is not None:
        cls, loc = error
        event["error"] = {"class": cls, "loc": loc}
    return event


def _send_telemetry(event: dict) -> None:
    """Transmit a telemetry event. Silent on every failure mode — telemetry
    must never break a real run.

    Two transport modes, picked by what's in settings.json:

      1. `telemetry_endpoint` like `https://...`  — generic HTTPS POST. The
         endpoint receives the event as a JSON body. Used when we eventually
         deploy a Cloudflare Worker / Vercel function / Fly service.

      2. `telemetry_endpoint` like `github://owner/repo` — GitHub Contents
         API commit. `telemetry_github_token` carries the PAT (contents:write
         on the target repo). Each event becomes a file at
         `events/YYYY-MM-DD/<timestamp>-<install_id_prefix>.json` via a
         single PUT call. This is the F&F-window setup.

    Either way the JSON shape is identical, so migration between modes is
    a single field change in settings.json.
    """
    import base64
    import urllib.request
    from urllib.parse import urlparse

    endpoint = _telemetry_endpoint()
    if not endpoint:
        return

    try:
        # github://owner/repo transport
        if endpoint.startswith("github://"):
            settings = _load_settings()
            token = settings.get("telemetry_github_token")
            if not token:
                return
            spec = endpoint[len("github://"):]
            if "/" not in spec:
                return
            owner, repo = spec.split("/", 1)
            now = datetime.now(timezone.utc)
            day = now.strftime("%Y-%m-%d")
            stamp = now.strftime("%Y-%m-%dT%H-%M-%S")
            install_id = str(event.get("install_id", "unknown"))[:8]
            filename = f"{stamp}-{install_id}.json"
            path = f"events/{day}/{filename}"
            body_json = json.dumps(event, indent=2, sort_keys=True)
            url = f"https://api.github.com/repos/{owner}/{repo}/contents/{path}"
            payload = json.dumps({
                "message": f"telemetry: {event.get('subcommand', 'run')}",
                "content": base64.b64encode(body_json.encode("utf-8")).decode("ascii"),
                "committer": {
                    "name": "tokenmin-telemetry",
                    "email": "telemetry@tokenmin.ai",
                },
            }).encode("utf-8")
            req = urllib.request.Request(url, data=payload, method="PUT")
            req.add_header("Authorization", f"Bearer {token}")
            req.add_header("Accept", "application/vnd.github+json")
            req.add_header("Content-Type", "application/json")
            urllib.request.urlopen(req, timeout=3)
            return

        # Plain HTTPS endpoint
        scheme = urlparse(endpoint).scheme
        if scheme != "https":
            return
        payload = json.dumps(event).encode("utf-8")
        req = urllib.request.Request(endpoint, data=payload, method="POST")
        req.add_header("Content-Type", "application/json")
        urllib.request.urlopen(req, timeout=2)
    except Exception:
        pass


def _maybe_telemetry_consent() -> None:
    """First-run consent ask for public-scanner installs. F&F invitees have
    `telemetry: on` pre-set by the installer and skip this. Public users see
    this on their first interactive `tokenmin` invocation."""
    s = _load_settings()
    if s.get("telemetry") in ("on", "off"):
        return  # already decided
    if s.get("telemetry_consent_asked"):
        return  # asked once before — don't nag
    if not (sys.stdin.isatty() and sys.stderr.isatty()):
        return  # non-interactive: leave it null, defer to a later interactive run
    c = _C(_ansi_supported())
    print(f"\n  {c.BOLD}{c.MAGENTA}Tokenmin{c.RESET} can send anonymous usage stats to improve the rule base.", file=sys.stderr)
    print(f"  {c.DIM}This is separate from the audit snapshot you already control with --submit-url.{c.RESET}", file=sys.stderr)
    print(file=sys.stderr)
    print(f"  {c.BOLD}What's sent (per invocation):{c.RESET}", file=sys.stderr)
    print(f"    - tokenmin version + platform + python version", file=sys.stderr)
    print(f"    - which subcommand you ran (run / watch / show / demo / etc.)", file=sys.stderr)
    print(f"    - which detectors fired (id only, never the values)", file=sys.stderr)
    print(f"    - session count bucketed (0 / 1-10 / 11-100 / 101+)", file=sys.stderr)
    print(f"    - model families used (Opus/Sonnet/Haiku — no version IDs)", file=sys.stderr)
    print(f"    - distribution buckets (cache-hit / parallelism / cost / context pressure)", file=sys.stderr)
    print(f"      so we can discover NEW optimization patterns we don't catch yet", file=sys.stderr)
    print(f"    - your setup 'signature' (has CLAUDE.md / hook count / MCP count, bucketed)", file=sys.stderr)
    print(f"    - error class + source line on exceptions (no message, no paths)", file=sys.stderr)
    print(f"    - a stable install_id (HMAC of your salt — can't be reversed to identify you)", file=sys.stderr)
    print(file=sys.stderr)
    print(f"  {c.BOLD}Never sent:{c.RESET} the snapshot, file paths, project names, raw errors, IP, email.", file=sys.stderr)
    print(f"  Inspect what would go: {c.CYAN}tokenmin telemetry dry-run{c.RESET}", file=sys.stderr)
    print(f"  Change anytime:        {c.CYAN}tokenmin telemetry on|off{c.RESET}", file=sys.stderr)
    print(file=sys.stderr)
    print(f"  Enable? [y/N] ", end="", file=sys.stderr)
    sys.stderr.flush()
    try:
        ans = input().strip().lower()
    except (EOFError, KeyboardInterrupt):
        ans = ""
    s["telemetry"] = "on" if ans in ("y", "yes") else "off"
    s["telemetry_consent_asked"] = True
    _save_settings(s)
    c = _C(_ansi_supported())
    if s["telemetry"] == "on":
        print(f"  {c.GREEN}✓{c.RESET} telemetry enabled. {c.DIM}Disable anytime with `tokenmin telemetry off`.{c.RESET}", file=sys.stderr)
    else:
        print(f"  {c.DIM}telemetry disabled. Enable later with `tokenmin telemetry on`.{c.RESET}", file=sys.stderr)
    print(file=sys.stderr)


def _telemetry_cmd(args: list[str]) -> int:
    """tokenmin telemetry on|off|status|dry-run"""
    import argparse as _ap
    sp = _ap.ArgumentParser(prog="tokenmin telemetry")
    sp.add_argument("action", choices=("on", "off", "status", "dry-run"))
    a = sp.parse_args(args)
    c = _C(_ansi_supported())
    s = _load_settings()
    if a.action == "on":
        s["telemetry"] = "on"
        s["telemetry_consent_asked"] = True
        _save_settings(s)
        print(f"{c.GREEN}✓{c.RESET} telemetry enabled")
        return 0
    if a.action == "off":
        s["telemetry"] = "off"
        s["telemetry_consent_asked"] = True
        _save_settings(s)
        print(f"{c.GREEN}✓{c.RESET} telemetry disabled")
        return 0
    if a.action == "status":
        state = s.get("telemetry", "unset (will ask on first interactive run)")
        endpoint = s.get("telemetry_endpoint") or "(none configured — events not transmitted)"
        env_override = "yes" if os.environ.get("TOKENMIN_NO_TELEMETRY") else "no"
        print(f"  telemetry:           {c.BOLD}{state}{c.RESET}")
        print(f"  endpoint:            {endpoint}")
        print(f"  TOKENMIN_NO_TELEMETRY env override: {env_override}")
        print(f"  install_id:          {_telemetry_install_id()}")
        print(f"  settings file:       {_SETTINGS_PATH}")
        print()
        print(f"  inspect what gets sent: {c.CYAN}tokenmin telemetry dry-run{c.RESET}")
        return 0
    if a.action == "dry-run":
        # Representative event for a tokenmin run, including the discovery
        # fields (metrics + setup_signature) the rule-base researcher uses
        # to find candidate new detectors empirically.
        event = _build_telemetry_event(
            subcommand="run",
            findings_fired=["model_overspend", "no_output_style", "long_sessions_no_clear"],
            session_count=57,
            models_used_families={"opus": 52, "sonnet": 3},
            snapshot_summary={
                "sessions": 57,
                "user_turns": 4100,
                "input_tokens": 1_200_000,
                "output_tokens": 15_000_000,
                "cache_read_tokens": 80_000_000,
                "cache_write_tokens": 2_000_000,
                "avg_tools_per_turn": 1.4,
                "total_cost_usd": 6860,
                "top_tools": [{"name": "Bash", "share": 0.40}],
            },
            config_summary={
                "has_global_claude_md": False,
                "global_claude_md_lines": 0,
                "global_hook_count": 0,
                "mcp_servers": 1,
                "custom_agents": 0,
                "custom_skills": 0,
                "output_style": None,
                "enable_tool_search": None,
            },
        )
        print("# This is the EXACT payload tokenmin would POST to the endpoint.")
        print("# Telemetry is only transmitted if `telemetry: on` AND an endpoint is configured.")
        print("# Schema: tokenmin.telemetry.v1.")
        print(json.dumps(event, indent=2, sort_keys=True))
        return 0
    return 2


_VALID_PLANS = ("api", "pro", "max", "unknown")


def _billing_plan() -> str:
    """Current billing plan from settings.json. Defaults to 'unknown'."""
    s = _load_settings()
    plan = s.get("billing_plan", "unknown")
    return plan if plan in _VALID_PLANS else "unknown"


def _maybe_billing_plan_consent() -> None:
    """First interactive run asks how the user pays for Claude.

    Without this, tokenmin reports 'Est. cost $X' which is the API-equivalent
    cost — accurate at retail rates, but misleading on Pro/Max where the user
    actually pays a flat fee. Knowing the plan lets us frame savings in the
    right unit (dollars on API; % quota stretch on Pro/Max).

    Skips:
      - already set (any value in _VALID_PLANS other than 'unknown')
      - non-interactive (no tty) — leaves 'unknown' for a later run
      - explicit decline ('skip') — sets 'unknown' + a marker so we don't nag
    """
    s = _load_settings()
    if s.get("billing_plan") in ("api", "pro", "max"):
        return
    if s.get("billing_plan_asked"):
        return
    if not (sys.stdin.isatty() and sys.stderr.isatty()):
        return
    c = _C(_ansi_supported())
    print(f"\n  {c.BOLD}One quick question:{c.RESET} how do you pay for Claude?", file=sys.stderr)
    print(f"  {c.DIM}Tokenmin reports cost at API rates. On Claude Pro/Max (flat fee) those{c.RESET}", file=sys.stderr)
    print(f"  {c.DIM}numbers don't match your bill — knowing your plan lets us reframe them{c.RESET}", file=sys.stderr)
    print(f"  {c.DIM}as 'quota stretch' instead of dollar savings.{c.RESET}", file=sys.stderr)
    print(file=sys.stderr)
    print(f"    {c.CYAN}a{c.RESET}) Anthropic API (metered, pay per token)", file=sys.stderr)
    print(f"    {c.CYAN}p{c.RESET}) Claude Pro (~$20/mo flat)", file=sys.stderr)
    print(f"    {c.CYAN}m{c.RESET}) Claude Max (~$100-$200/mo flat)", file=sys.stderr)
    print(f"    {c.CYAN}s{c.RESET}) skip — leave as 'unknown' (you can set it later with `tokenmin plan <choice>`)", file=sys.stderr)
    print(file=sys.stderr)
    print(f"  Choose [a/p/m/s] ", end="", file=sys.stderr)
    sys.stderr.flush()
    try:
        ans = input().strip().lower()
    except (EOFError, KeyboardInterrupt):
        ans = ""
    plan_map = {"a": "api", "api": "api", "p": "pro", "pro": "pro", "m": "max", "max": "max"}
    s["billing_plan"] = plan_map.get(ans, "unknown")
    s["billing_plan_asked"] = True
    _save_settings(s)
    if s["billing_plan"] == "unknown":
        print(f"  {c.DIM}set to 'unknown'. Set later with `tokenmin plan api|pro|max`.{c.RESET}", file=sys.stderr)
    else:
        print(f"  {c.GREEN}✓{c.RESET} billing plan set to {c.BOLD}{s['billing_plan']}{c.RESET}", file=sys.stderr)
    print(file=sys.stderr)


def _maybe_stale_pricing_warning() -> None:
    """Warn when bundled pricing.json is older than its own stale threshold.

    Fires at most once per day (tracked in settings) so noisy runs don't repeat
    the warning. Quiet for subscription users (they don't see dollars anyway).
    """
    s = _load_settings()
    plan = s.get("billing_plan", "unknown")
    if plan in ("pro", "max"):
        return
    if not (sys.stderr.isatty()):
        return
    try:
        from pricing import is_stale, pricing_age_days, pricing_metadata  # type: ignore
    except ImportError:
        return
    if not is_stale():
        return
    last_warn = s.get("stale_pricing_last_warn", "")
    today = datetime.now(timezone.utc).date().isoformat()
    if last_warn == today:
        return
    age = pricing_age_days() or 0
    meta = pricing_metadata()
    c = _C(_ansi_supported())
    print(file=sys.stderr)
    print(f"  {c.YELLOW}note:{c.RESET} bundled pricing data is {c.BOLD}{age} days{c.RESET} old "
          f"(updated {meta.get('last_updated', '?')}).", file=sys.stderr)
    print(f"  {c.DIM}Dollar numbers may not match Anthropic's current rates.{c.RESET}", file=sys.stderr)
    print(f"  {c.DIM}Run `tokenmin --update` to pull the latest, or check {meta.get('source', 'anthropic.com/pricing')}.{c.RESET}", file=sys.stderr)
    print(file=sys.stderr)
    s["stale_pricing_last_warn"] = today
    _save_settings(s)


def _plan_cmd(args: list[str]) -> int:
    """tokenmin plan <api|pro|max|unknown|status>"""
    import argparse as _ap
    sp = _ap.ArgumentParser(prog="tokenmin plan", description="Set your Claude billing plan so savings get framed in the right units.")
    sp.add_argument("action", choices=_VALID_PLANS + ("status",))
    a = sp.parse_args(args)
    c = _C(_ansi_supported())
    s = _load_settings()
    if a.action == "status":
        plan = s.get("billing_plan", "unknown")
        asked = "yes" if s.get("billing_plan_asked") else "no (will prompt on next interactive run)"
        print(f"  billing plan:  {c.BOLD}{plan}{c.RESET}")
        print(f"  consent asked: {asked}")
        print(f"  settings file: {_SETTINGS_PATH}")
        print()
        print(f"  change with:   {c.CYAN}tokenmin plan api|pro|max|unknown{c.RESET}")
        return 0
    s["billing_plan"] = a.action
    s["billing_plan_asked"] = True
    _save_settings(s)
    print(f"{c.GREEN}✓{c.RESET} billing plan set to {c.BOLD}{a.action}{c.RESET}")
    return 0


_CANONICAL_INSTALL_DIR = Path.home() / ".tokenmin"


def _is_real_install(root: Path) -> bool:
    """Refuse to uninstall from anywhere except the canonical install root.

    A real install always lives at ~/.tokenmin (install.sh hardcodes DEST).
    Dev trees (a git clone of the source repo where someone imports tokenmin.py
    for testing) are the one place `_install_dir()` resolves to a non-install
    path via __file__ — deleting that would nuke the user's working copy.
    """
    try:
        return root.resolve() == _CANONICAL_INSTALL_DIR.resolve()
    except OSError:
        return False


def _shell_rc_candidates() -> list[Path]:
    """Files the installer may have touched. Order matches install.sh."""
    home = Path.home()
    return [
        home / ".zshrc",
        home / ".bashrc",
        home / ".bash_profile",
        home / ".config" / "fish" / "config.fish",
    ]


def _strip_installer_marker(rc: Path) -> bool:
    """Remove '# Added by tokenmin installer on <ts>' + the line right after it.

    install.sh always writes the marker immediately followed by the PATH export
    line, so dropping the pair is safe and doesn't touch anything the user
    added by hand. Returns True iff the file was modified.
    """
    try:
        original = rc.read_text()
    except OSError:
        return False
    lines = original.splitlines(keepends=True)
    out: list[str] = []
    skip_next = False
    changed = False
    for line in lines:
        if skip_next:
            skip_next = False
            changed = True
            continue
        if line.lstrip().startswith("# Added by tokenmin installer on"):
            skip_next = True
            changed = True
            continue
        out.append(line)
    if not changed:
        return False
    new = "".join(out)
    # Collapse a trailing run of blank lines so we don't leave an ever-growing
    # gap after repeated install/uninstall cycles.
    while new.endswith("\n\n\n"):
        new = new[:-1]
    try:
        rc.write_text(new)
    except OSError:
        return False
    return True


def _uninstall_claude_plugin() -> bool:
    """Best-effort removal of the Claude Code plugin registration."""
    import shutil as _sh
    import subprocess as _sp
    if not _sh.which("claude"):
        return False
    try:
        listed = _sp.run(["claude", "plugin", "list"], capture_output=True, text=True, timeout=10)
    except (OSError, _sp.TimeoutExpired):
        return False
    if "tokenmin@tokenmin" not in (listed.stdout or ""):
        return False
    try:
        _sp.run(["claude", "plugin", "uninstall", "tokenmin@tokenmin"], capture_output=True, timeout=10)
        _sp.run(["claude", "plugin", "marketplace", "remove", "tokenmin"], capture_output=True, timeout=10)
    except (OSError, _sp.TimeoutExpired):
        return False
    return True


def _uninstall(args: list[str]) -> int:
    """Remove the install dir, symlink, shell-rc PATH lines, and Claude Code plugin.

    One-line success output matches the install greet:
        tokenmin 0.12.2 uninstalled
    Pass --verbose / -v to see each step. Pass --dry-run to plan without acting.
    """
    import argparse as _ap
    sp = _ap.ArgumentParser(
        prog="tokenmin uninstall",
        description="Remove tokenmin. Strips installer-added PATH lines + Claude Code plugin.",
    )
    sp.add_argument("--yes", "-y", action="store_true", help="Skip the confirmation prompt")
    sp.add_argument("--verbose", "-v", action="store_true", help="Print each step (default is one line)")
    sp.add_argument("--dry-run", action="store_true", help="Show the plan; touch nothing")
    a = sp.parse_args(args)

    verbose = a.verbose or bool(os.environ.get("TOKENMIN_VERBOSE"))
    c = _C(_ansi_supported())

    root = _install_dir()
    bin_link = Path.home() / ".local" / "bin" / "tokenmin"

    # Safety: refuse to uninstall from a dev/source tree. The only legitimate
    # install root is ~/.tokenmin (install.sh hardcodes it). If we resolved to
    # somewhere else, the user is running uninstall from a clone — bail.
    if not _is_real_install(root):
        print(
            f"tokenmin uninstall: refusing to remove {root}\n"
            f"  this doesn't look like a real install (expected {_CANONICAL_INSTALL_DIR}).\n"
            f"  if you want to remove a real install, run `~/.tokenmin/tokenmin uninstall`.",
            file=sys.stderr,
        )
        return 4

    # Capture version BEFORE we delete anything so the success line is honest.
    version_str = ""
    version_file = root / "VERSION"
    if version_file.is_file():
        try:
            version_str = " " + version_file.read_text().strip()
        except OSError:
            pass

    # Build the plan. Each action returns (ok, detail).
    import shutil as _sh
    plan: list[tuple[str, callable]] = []

    if bin_link.is_symlink():
        target_str = ""
        try:
            target_str = str(bin_link.resolve())
        except OSError:
            pass
        # Resolve root too — on macOS, $TMPDIR is /var/folders/... which
        # resolves to /private/var/folders/..., so an unresolved prefix check
        # silently fails to recognize our own symlink.
        try:
            root_str = str(root.resolve())
        except OSError:
            root_str = str(root)
        points_at_us = target_str.startswith(root_str)
        if points_at_us:
            def _rm_link() -> tuple[bool, str]:
                try:
                    bin_link.unlink()
                    return True, str(bin_link)
                except OSError as exc:
                    return False, f"{bin_link}: {exc}"
            plan.append((f"symlink {bin_link}", _rm_link))
        else:
            plan.append((
                f"symlink {bin_link} (left alone — points at {target_str}, not us)",
                lambda: (True, "left alone"),
            ))

    if root.exists():
        def _rm_root() -> tuple[bool, str]:
            try:
                _sh.rmtree(root)
                return True, str(root)
            except OSError as exc:
                return False, f"{root}: {exc}"
        plan.append((f"install dir {root}", _rm_root))

    for rc in _shell_rc_candidates():
        if rc.exists():
            try:
                content = rc.read_text()
            except OSError:
                continue
            if "# Added by tokenmin installer on" in content:
                def _strip(_rc=rc) -> tuple[bool, str]:
                    ok = _strip_installer_marker(_rc)
                    return ok, f"PATH line removed from {_rc}"
                plan.append((f"shell-rc PATH line in {rc}", _strip))

    # Claude Code plugin — only add if it's actually registered.
    import shutil as _sh2
    import subprocess as _sp2
    if _sh2.which("claude"):
        try:
            listed = _sp2.run(["claude", "plugin", "list"], capture_output=True, text=True, timeout=10)
            if "tokenmin@tokenmin" in (listed.stdout or ""):
                def _rm_plugin() -> tuple[bool, str]:
                    ok = _uninstall_claude_plugin()
                    return ok, "Claude Code plugin tokenmin@tokenmin"
                plan.append(("Claude Code plugin tokenmin@tokenmin", _rm_plugin))
        except (OSError, _sp2.TimeoutExpired):
            pass

    if not plan:
        print(f"tokenmin{version_str}: nothing to uninstall (no install dir, symlink, PATH line, or plugin found)")
        return 0

    # Dry-run / verbose / interactive confirmation share the same plan listing.
    if a.dry_run or verbose:
        print("tokenmin uninstall plan:", file=sys.stderr)
        for label, _ in plan:
            print(f"  - {label}", file=sys.stderr)
        if a.dry_run:
            print(f"tokenmin{version_str} uninstall dry-run — nothing removed", file=sys.stderr)
            return 0

    if not a.yes:
        if not sys.stdin.isatty():
            print("tokenmin uninstall: refusing non-interactive run without --yes", file=sys.stderr)
            return 2
        if not verbose:
            prompt = f"remove tokenmin ({len(plan)} item{'s' if len(plan) != 1 else ''})? [y/N] "
        else:
            prompt = "remove? [y/N] "
        try:
            ans = input(prompt).strip().lower()
        except (EOFError, KeyboardInterrupt):
            ans = ""
        if ans not in ("y", "yes"):
            print("aborted.", file=sys.stderr)
            return 1

    # Move CWD out of root if we're sitting inside it, otherwise rmtree fails.
    try:
        cwd = Path.cwd().resolve()
        if str(cwd).startswith(str(root.resolve())):
            os.chdir(Path.home())
    except OSError:
        pass

    failures: list[str] = []
    for label, action in plan:
        ok, detail = action()
        if ok:
            if verbose:
                print(f"  {c.GREEN}✓{c.RESET} {detail}", file=sys.stderr)
        else:
            failures.append(detail)
            print(f"  {c.YELLOW}!{c.RESET} {detail}", file=sys.stderr)

    if failures:
        print(
            f"tokenmin{version_str} uninstalled with {len(failures)} warning(s) — "
            f"rerun with -v for details",
            file=sys.stderr,
        )
        return 1
    print(f"tokenmin{version_str} uninstalled")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
