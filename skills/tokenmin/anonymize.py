# SPDX-License-Identifier: Apache-2.0
"""Scrub PII, paths, secrets, and identifying labels from analyzer output.

Runs BEFORE any report or JSON is written. The contract: nothing
user-identifying or environment-specific survives this pass. If it might,
add a rule here.

Honest naming: this produces a **pseudonymized** snapshot, not an anonymous
one. Hashes are stable across runs so the engine can correlate ("re-read
this file 12x") — that stability also lets a determined adversary with
many snapshots fingerprint a user. Trade-off accepted on purpose; if you
want stronger guarantees, use --strict (per-run salt).
"""
from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
from pathlib import Path

# Hashes are HMAC-SHA256(salt, value), NOT plain SHA-256. The salt is:
#
#   1. Per-INSTALL: 32 random bytes generated on first run at
#      $TOKENMIN_SALT_PATH (default ~/.tokenmin/.salt, chmod 600).
#      This kills the rainbow-table attack: an adversary who guesses
#      "~/.ssh/known_hosts" can't precompute its hash without the salt.
#      Cross-snapshot correlation still works WITHIN a user's data because
#      the salt is stable, but cross-USER correlation is broken — same
#      path gives different hashes for different users.
#
#   2. Per-RUN (optional, TOKENMIN_STRICT_ANONYMIZE=1): an additional
#      random salt mixed in per process. Breaks WITHIN-user cross-run
#      correlation too — strictly anonymous, at the cost of the engine
#      losing "same file re-read across days" findings.
#
# Truncation length: 16 hex chars = 64 bits. Collision probability is
# negligible for any realistic corpus (birthday-bound ~2^32 unique inputs
# before any collision becomes likely).

_HASH_HEX_LEN = 16
_STRICT = os.environ.get("TOKENMIN_STRICT_ANONYMIZE") == "1"
_RUN_SALT = secrets.token_bytes(32) if _STRICT else b""


def _salt_path() -> Path:
    """Where the per-install salt lives. Override with TOKENMIN_SALT_PATH."""
    env = os.environ.get("TOKENMIN_SALT_PATH")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".tokenmin" / ".salt"


def _load_or_create_salt() -> bytes:
    """Read the per-install salt, generating + persisting it on first run.

    32 random bytes, written with mode 0600. If we can't write (read-only
    install, permission denied), fall back to an in-memory ephemeral salt
    — pseudonymization still works for this run, but cross-run correlation
    is lost. We log this fact via stderr once.
    """
    path = _salt_path()
    try:
        if path.exists():
            data = path.read_bytes()
            if len(data) >= 32:
                return data[:32]
    except OSError:
        pass
    # Generate fresh.
    salt = secrets.token_bytes(32)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # O_CREAT | O_WRONLY | O_EXCL with 0600 — atomic, refuses to overwrite.
        fd = os.open(str(path), os.O_CREAT | os.O_WRONLY | os.O_EXCL, 0o600)
        try:
            os.write(fd, salt)
        finally:
            os.close(fd)
    except FileExistsError:
        # Race: another process created it. Read and use that one.
        try:
            return path.read_bytes()[:32]
        except OSError:
            return salt  # fall through to ephemeral
    except OSError:
        # Couldn't write — ephemeral salt for this run only.
        import sys
        print(
            "tokenmin: warning: could not persist anonymization salt at "
            f"{path}; using ephemeral salt for this run. Cross-run "
            "correlation will be lost.",
            file=sys.stderr,
        )
    return salt


_INSTALL_SALT = _load_or_create_salt()


# --- secret patterns --------------------------------------------------------
# Order matters — apply specific patterns before greedy ones.
PATTERNS: list[tuple[re.Pattern, str]] = [
    # Anthropic / OpenAI / GitHub / Slack / AWS / Google
    (re.compile(r"sk-ant-[A-Za-z0-9_-]{20,}"), "<ANTHROPIC_KEY>"),
    (re.compile(r"sk-proj-[A-Za-z0-9_-]{20,}"), "<OPENAI_KEY>"),
    (re.compile(r"sk-[A-Za-z0-9]{20,}"), "<API_KEY>"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"), "<GITHUB_TOKEN>"),
    (re.compile(r"github_pat_[A-Za-z0-9_]{20,}"), "<GITHUB_TOKEN>"),
    (re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"), "<SLACK_TOKEN>"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "<AWS_KEY>"),
    (re.compile(r"\bASIA[0-9A-Z]{16}\b"), "<AWS_STS>"),
    (re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"), "<GOOGLE_KEY>"),
    # Stripe
    (re.compile(r"\b(?:sk|pk|rk)_(?:live|test)_[A-Za-z0-9]{20,}\b"), "<STRIPE_KEY>"),
    # npm tokens
    (re.compile(r"\bnpm_[A-Za-z0-9]{36,}\b"), "<NPM_TOKEN>"),
    # JWT (header.payload.signature)
    (re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"), "<JWT>"),
    # Google service-account JSON private-key blocks
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"), "<PRIVATE_KEY_BLOCK>"),
    # Generic bearer (handles trailing whitespace / newlines)
    (re.compile(r"Bearer[\s\r\n]+[A-Za-z0-9._\-]{16,}"), "Bearer <TOKEN>"),
    # High-entropy generic key heuristic: 32+ chars of base64-ish, surrounded by non-key chars.
    # Conservative — only fires on long runs with mixed case + digits.
    (
        re.compile(
            r"(?<![A-Za-z0-9_\-])"
            r"(?=[A-Za-z0-9_\-]*[A-Z])(?=[A-Za-z0-9_\-]*[a-z])(?=[A-Za-z0-9_\-]*\d)"
            r"[A-Za-z0-9_\-]{32,}"
            r"(?![A-Za-z0-9_\-])"
        ),
        "<HIGH_ENTROPY_TOKEN>",
    ),
    # Emails + IPs
    (re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b"), "<EMAIL>"),
    (re.compile(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b"), "<IP>"),
    # User home (raw + URL-encoded)
    (re.compile(r"/Users/[^/\s\"']+"), "/Users/<USER>"),
    (re.compile(r"/home/[^/\s\"']+"), "/home/<USER>"),
    (re.compile(r"C:\\Users\\[^\\\s\"']+"), r"C:\\Users\\<USER>"),
    (re.compile(r"%2FUsers%2F[^/%\s\"']+", re.IGNORECASE), "%2FUsers%2F<USER>"),
    (re.compile(r"%2Fhome%2F[^/%\s\"']+", re.IGNORECASE), "%2Fhome%2F<USER>"),
    # Mangled Claude Code project dir form: -Users-someone-...
    (re.compile(r"\-Users\-[^\-\s\"']+"), "-Users-<USER>"),
    (re.compile(r"\-home\-[^\-\s\"']+"), "-home-<USER>"),
]

# Filename-like sequences. Matches paths whether or not they start with `/`,
# so a path fragment surviving an earlier substitution (e.g. across a space
# or quote) gets caught on a second sweep. The new behavior hashes the WHOLE
# path — no surviving suffix. Use scrub_path_keep_suffix for the legacy
# debug shape behind --keep-suffix.
PATH_LIKE = re.compile(r"/?(?:[A-Za-z0-9._\-]+/){2,}[A-Za-z0-9._\-]+")
# Mangled Claude Code project dir names (multi-segment, dash-separated paths).
MANGLED_PATH_LIKE = re.compile(r"\-[A-Za-z0-9]+(?:\-[A-Za-z0-9._]+){3,}")


def _hash(s: str) -> str:
    """HMAC-SHA256 with the per-install salt (+ optional per-run salt), truncated to 16 hex.

    Cryptographically defensible against rainbow-table de-anonymization:
    without the salt, an adversary can't reverse a hash by guessing common
    inputs.
    """
    key = _INSTALL_SALT + _RUN_SALT
    return hmac.new(key, s.encode("utf-8"), hashlib.sha256).hexdigest()[:_HASH_HEX_LEN]


# Backward-compat shim (kept for any external callers / tests).
_hash8 = _hash


# Defense-in-depth caps. Pathological inputs (zip-bomb session files, multi-MB
# JSONL lines, deeply nested structures) shouldn't be able to hang the scrubber
# in catastrophic regex backtracking or OOM the process.
_MAX_SCRUB_LEN = 64 * 1024   # 64 KiB per string — anything larger is truncated.
_MAX_DICT_DEPTH = 8          # already existed; documented now.


def scrub_text(text: str) -> str:
    """Scrub a free-text string. Idempotent.

    Inputs over _MAX_SCRUB_LEN are truncated with a marker. This prevents
    regex catastrophic backtracking on adversarial inputs (e.g. session
    files an attacker can plant in ~/.claude/projects/).
    """
    if not text:
        return text
    if len(text) > _MAX_SCRUB_LEN:
        text = text[:_MAX_SCRUB_LEN] + "<truncated_by_scrubber>"
    out = text
    for pat, repl in PATTERNS:
        out = pat.sub(repl, out)
    return out


def scrub_label(label: str) -> str:
    """Whole-string hash for identifiers we don't want to leak in any form.

    Use for: project field, MCP server names, custom agent / skill / command
    filenames, custom subagent_type values. Returns `<id:abcd1234>` — caller
    keeps a tag prefix if it wants to distinguish kinds.
    """
    if not label:
        return label
    return f"<id:{_hash8(label)}>"


def scrub_path(path: str | Path) -> str:
    """Replace a path with a stable hash. The filename suffix is NOT preserved
    (filename leakage was a red-team finding). Use scrub_path_keep_suffix for
    the legacy debug shape.
    """
    p = str(path)
    if not p:
        return p
    return f"<path:{_hash8(p)}>"


def scrub_path_keep_suffix(path: str | Path) -> str:
    """LEGACY: preserves the last segment for debug readability. Not used by
    default — exposes filename suffixes across snapshots. Available behind
    --keep-suffix when a human needs to read the snapshot."""
    p = str(path)
    if not p:
        return p
    last = Path(p).name or "_"
    return f"<path:{_hash8(p)}>/{last}"


_PATH_SCRUBBER = scrub_path  # rebound when --keep-suffix is set


def use_keep_suffix(value: bool) -> None:
    """Toggle path-scrub mode at runtime. Called once from the CLI."""
    global _PATH_SCRUBBER
    _PATH_SCRUBBER = scrub_path_keep_suffix if value else scrub_path


def scrub_paths_in_text(text: str) -> str:
    """Replace anything that looks like a multi-segment path."""
    if not text:
        return text
    out = PATH_LIKE.sub(lambda m: _PATH_SCRUBBER(m.group(0)), text)
    out = MANGLED_PATH_LIKE.sub(lambda m: f"<path:{_hash8(m.group(0))}>", out)
    return out


def scrub_dict(d: dict, _depth: int = 0) -> dict:
    """Recursively scrub a dict. Caps recursion depth.

    Dict KEYS get the same scrub as string VALUES (paths + secret patterns).
    This matters for fields like `files_read` whose Counter keys ARE file
    paths — without this, the path-scrub never runs on them.
    """
    if _depth > 8:
        return {"_truncated": True}
    out: dict = {}
    for k, v in d.items():
        nk = scrub_text(scrub_paths_in_text(str(k)))
        if isinstance(v, dict):
            out[nk] = scrub_dict(v, _depth + 1)
        elif isinstance(v, list):
            out[nk] = [scrub_value(x, _depth + 1) for x in v]
        else:
            out[nk] = scrub_value(v, _depth + 1)
    return out


def scrub_value(v, _depth: int = 0):
    if isinstance(v, str):
        return scrub_text(scrub_paths_in_text(v))
    if isinstance(v, dict):
        return scrub_dict(v, _depth + 1)
    if isinstance(v, list):
        return [scrub_value(x, _depth + 1) for x in v]
    return v


# --- selfcheck --------------------------------------------------------------

SELFCHECK_INPUT = {
    "examples": [
        "API: sk-ant-abc123def456ghi789jkl0123",
        "email: rick@rmwcommerce.com, ip 10.0.0.1",
        "path /Users/richardmwatson/Documents/Channable_Wiki/backlog.md",
        "url-encoded %2FUsers%2Frickwatson%2F",
        "mangled -Users-richardmwatson-Documents-Channable-Wiki",
        "Bearer\n  xoxb-123456789012-abcdefghij",
        "JWT eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjMifQ.signaturePartHere1234",
        "stripe sk_live_abcdefghij1234567890XYZ",
        "high entropy: AbcDef123456789012XyzGhi3456789012",
    ],
    "project": "-Users-richardmwatson-Documents-Channable_Wiki",
    "mcp_servers": ["pipedrive", "slack", "intuit_quickbooks"],
    "custom_skills": ["client-prep-watson-weekly", "wiki-context"],
}


def selfcheck() -> dict:
    """Run a deterministic anonymization over a known input.

    Used by `tokenmin --selfcheck` so previewers can see what the scrubber
    actually does without having to read Python. Output is a dict mapping
    each example input to its scrubbed output.
    """
    out = {
        "examples": [
            {"input": s, "output": scrub_text(scrub_paths_in_text(s))}
            for s in SELFCHECK_INPUT["examples"]
        ],
        "labels": {
            "project": scrub_label(SELFCHECK_INPUT["project"]),
            "mcp_servers": [scrub_label(x) for x in SELFCHECK_INPUT["mcp_servers"]],
            "custom_skills": [scrub_label(x) for x in SELFCHECK_INPUT["custom_skills"]],
        },
        "strict_mode": _STRICT,
    }
    return out
