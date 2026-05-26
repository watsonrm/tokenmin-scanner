#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Property tests for the Tokenmin scrubber.

Stdlib-only (unittest, subprocess, tempfile). No third-party deps so CI is a
single `python3 tests/test_scrubber.py`.

Tests fall into two buckets:

  1. Property tests on anonymize.py directly: behaviors that hold regardless
     of the salt value (idempotence, salt-sensitivity, ReDoS cap, secret
     coverage). These can run with any salt.

  2. Determinism tests via the CLI: with a FIXED test salt fixture, the
     `--selfcheck` JSON output is bit-stable and we diff against the
     checked-in expected output. This catches "future commit changed the
     scrubber" regressions.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SKILL_DIR = REPO_ROOT / "skills" / "tokenmin"
FIXTURES = REPO_ROOT / "tests" / "fixtures"
TEST_SALT = FIXTURES / "test.salt"
EXPECTED_SELFCHECK = FIXTURES / "selfcheck.expected.json"
TOKENMIN_BIN = REPO_ROOT / "tokenmin"

# Make anonymize importable in-process for the property tests. Set the test
# salt BEFORE importing so _load_or_create_salt uses our fixture.
os.environ["TOKENMIN_SALT_PATH"] = str(TEST_SALT)
sys.path.insert(0, str(SKILL_DIR))

import anonymize  # noqa: E402  (after path setup)


class PropertyTests(unittest.TestCase):
    """Behaviors that hold regardless of the salt value."""

    def test_idempotent_scrub(self):
        """Running the scrubber twice produces the same output as once."""
        cases = [
            "rick@rmwcommerce.com",
            "sk-ant-abc123def456ghi789jkl0123",
            "/Users/richardmwatson/Documents/foo.md",
            "no secrets here",
            "",
        ]
        for case in cases:
            once = anonymize.scrub_text(anonymize.scrub_paths_in_text(case))
            twice = anonymize.scrub_text(anonymize.scrub_paths_in_text(once))
            self.assertEqual(once, twice, f"non-idempotent for {case!r}")

    def test_secret_patterns_caught(self):
        """Every named secret pattern in the input gets replaced.

        Test values are constructed at runtime by concatenation so the literal
        key-shaped strings never appear in source — otherwise GitHub's secret
        scanner blocks the push. They still exercise the same regexes at
        runtime because Python evaluates the concatenation before regex match.
        """
        # input -> expected MARKER in the output
        cases = [
            ("sk-ant-" + "abc123def456ghi789jkl0123", "<ANTHROPIC_KEY>"),
            ("sk-proj-" + "abc123def456ghi789jkl0123", "<OPENAI_KEY>"),
            ("ghp_" + "abcdef1234567890ABCDEFG", "<GITHUB_TOKEN>"),
            ("github_pat_" + "abcdef1234567890ABCDE", "<GITHUB_TOKEN>"),
            ("xoxb-" + "123456789012-abcdefghij", "<SLACK_TOKEN>"),
            ("AKIA" + "IOSFODNN7EXAMPLE", "<AWS_KEY>"),
            ("ASIA" + "IOSFODNN7EXAMPLE", "<AWS_STS>"),
            ("AIza" + "SyA1234567890ABCDEFGHIJKLMNOPQRSTUV", "<GOOGLE_KEY>"),
            ("sk_" + "live_" + "abc123def456ghi789jkl0123", "<STRIPE_KEY>"),
            ("npm_" + "abcdefghijklmnopqrstuvwxyzABCDEFGHIJ", "<NPM_TOKEN>"),
            (
                "eyJ" + "hbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjMifQ.signaturePartHere1234",
                "<JWT>",
            ),
            ("rick@" + "rmwcommerce.com", "<EMAIL>"),
            ("10.0.0.1", "<IP>"),
            ("Bearer " + "abcdefghijklmnopqrst", "Bearer <TOKEN>"),
            ("Bearer\n  " + "abcdefghijklmnopqrst", "Bearer <TOKEN>"),
        ]
        for raw, marker in cases:
            out = anonymize.scrub_text(raw)
            self.assertIn(marker, out, f"{raw!r} did not produce {marker!r}: {out!r}")
            self.assertNotIn(raw, out, f"raw still appears in {out!r}")

    def test_paths_get_hashed_whole_string(self):
        """File paths produce <path:HASH> with no filename suffix."""
        paths = [
            "/Users/x/Library/CloudStorage/foo/bar/baz.md",
            "/home/x/projects/secret/secrets.py",
            "C:\\Users\\Bob\\Documents\\plan.docx",
        ]
        for p in paths:
            h = anonymize.scrub_path(p)
            self.assertTrue(h.startswith("<path:"), f"{p!r} -> {h!r}")
            self.assertTrue(h.endswith(">"), f"{p!r} -> {h!r}")
            # No filename suffix should leak.
            self.assertNotIn(".md", h)
            self.assertNotIn(".py", h)
            self.assertNotIn(".docx", h)

    def test_user_home_patterns(self):
        for raw in (
            "/Users/richardmwatson/foo",
            "/home/rick/foo",
            r"C:\Users\Bob\foo",
            "%2FUsers%2Frickwatson%2F",
            "%2Fhome%2Frick%2F",
            "-Users-richardmwatson-Documents-foo-bar",
        ):
            out = anonymize.scrub_text(raw)
            # User segment should be replaced by either <USER> or a path hash.
            self.assertTrue(
                "<USER>" in out or "<path:" in out,
                f"{raw!r} survived as {out!r}",
            )

    def test_salt_sensitivity(self):
        """Same input + different install-salt -> different hash."""
        # We can't easily change the loaded _INSTALL_SALT without re-importing,
        # so test the CLI form: two fresh salts, two different hashes.
        s1 = self._run_selfcheck_with_fresh_salt()
        s2 = self._run_selfcheck_with_fresh_salt()
        p1 = s1["labels"]["project"]
        p2 = s2["labels"]["project"]
        self.assertNotEqual(p1, p2, "fresh salts produced the same project hash")

    def test_salt_stability(self):
        """Same install-salt -> same hash across runs (cross-run correlation)."""
        with tempfile.TemporaryDirectory() as td:
            salt_path = Path(td) / "salt"
            salt_path.write_bytes(b"\x42" * 32)
            os.chmod(salt_path, 0o600)
            a = self._run_selfcheck(salt_path)
            b = self._run_selfcheck(salt_path)
            self.assertEqual(a["labels"], b["labels"], "stable salt produced different hashes")

    def test_is_anthropic_export_name_strict(self):
        """--from latest / --watch-downloads must only match Anthropic exports,
        not other services that happen to include 'export' in the filename
        (LinkedIn, Twitter, generic). Real bug found in local testing."""
        import importlib
        sys.path.insert(0, str(SKILL_DIR))
        m = importlib.import_module("tokenmin")
        cases = [
            ("conversations.zip", True),
            ("data-export-20260520T143000.zip", True),
            ("claude-export-abc.zip", True),
            ("anthropic-export-xyz.zip", True),
            ("CLAUDE-EXPORT-X.ZIP", True),  # case insensitive
            ("Basic_LinkedInDataExport_08-21-2025.zip", False),
            ("twitter-data-export.zip", False),
            ("my-export.zip", False),
            ("Conversations.zip", True),
            ("random.zip", False),
        ]
        for name, expected in cases:
            self.assertEqual(
                m._is_anthropic_export_name(name), expected,
                f"_is_anthropic_export_name({name!r}) -> {m._is_anthropic_export_name(name)}, want {expected}"
            )

    def test_strip_ctl_blocks_ansi_injection(self):
        """ANSI escape codes and control chars get stripped from displayed text.
        Defense against adversarial filenames / project dirs that could hijack
        the terminal via escape sequences planted by an attacker with write
        access to ~/.claude/projects/."""
        import importlib
        # Import lazily — _strip_ctl lives in tokenmin.py which is also a CLI
        # script; tests/run.sh adds it to sys.path.
        sys.path.insert(0, str(SKILL_DIR))
        tokenmin_mod = importlib.import_module("tokenmin")
        cases = [
            ("clean", "clean"),
            ("\x1b[2Jhello", "hello"),                         # CSI screen-clear
            ("\x1b[31mred\x1b[0m", "red"),                     # CSI color
            ("title\x1b]2;HACKED\x07", "title"),               # OSC window-title
            ("title\x1b]0;X\x1b\\", "title"),                  # OSC with ST terminator
            ("with\x00null\x07bell", "withnullbell"),          # C0 controls
            ("tab\there", "tab\there"),                        # tab preserved
            ("line\nbreak", "line\nbreak"),                    # newline preserved
            ("c1\x9bcontrol", "c1control"),                    # C1 controls
            ("", ""),
        ]
        for raw, expected in cases:
            self.assertEqual(
                tokenmin_mod._strip_ctl(raw), expected,
                f"_strip_ctl({raw!r}) -> got {tokenmin_mod._strip_ctl(raw)!r}, want {expected!r}",
            )

    def test_redos_input_cap(self):
        """Pathologically long input is truncated rather than hanging the scrubber."""
        import time
        huge = "a" * (anonymize._MAX_SCRUB_LEN + 100_000)
        t0 = time.time()
        out = anonymize.scrub_text(huge)
        elapsed = time.time() - t0
        self.assertLess(elapsed, 1.0, f"scrub took {elapsed:.2f}s on {len(huge)}-byte input")
        self.assertIn("<truncated_by_scrubber>", out)
        self.assertLessEqual(len(out), anonymize._MAX_SCRUB_LEN + 100)  # marker overhead

    def _run_selfcheck_with_fresh_salt(self):
        with tempfile.NamedTemporaryFile(delete=False) as f:
            salt_path = Path(f.name)
        try:
            salt_path.unlink()  # let the CLI create it fresh
            return self._run_selfcheck(salt_path)
        finally:
            if salt_path.exists():
                salt_path.unlink()

    def _run_selfcheck(self, salt_path: Path) -> dict:
        env = dict(os.environ, TOKENMIN_SALT_PATH=str(salt_path), TOKENMIN_AUTOUPDATE="off")
        res = subprocess.run(
            ["bash", str(TOKENMIN_BIN), "--selfcheck"],
            capture_output=True, text=True, env=env, timeout=10,
        )
        self.assertEqual(res.returncode, 0, f"selfcheck failed: {res.stderr}")
        return json.loads(res.stdout)


class CLITests(unittest.TestCase):
    """CLI behaviors. Uses the fixed test salt for deterministic --selfcheck output.

    Each CLI invocation runs against a fresh sandbox HOME so the test suite
    never writes audit.log / last_run.json / .salt into the real
    ~/.tokenmin directory. Earlier versions of this class leaked real HOME
    via os.environ inheritance — running `bash tests/run.sh` would silently
    create or update files in the developer's actual install.
    """

    def setUp(self):
        self._sandbox_home = tempfile.mkdtemp(prefix="tokenmin-cli-test-")

    def tearDown(self):
        import shutil
        shutil.rmtree(self._sandbox_home, ignore_errors=True)

    def _run(self, *args, expect_ok=True, env_extra=None):
        env = dict(
            os.environ,
            HOME=self._sandbox_home,  # isolate audit log + last_run + .salt
            TOKENMIN_SALT_PATH=str(TEST_SALT),
            TOKENMIN_AUTOUPDATE="off",
        )
        if env_extra:
            env.update(env_extra)
        res = subprocess.run(
            ["bash", str(TOKENMIN_BIN), *args],
            capture_output=True, text=True, env=env, timeout=30,
        )
        if expect_ok:
            self.assertEqual(res.returncode, 0, f"{args}: stderr={res.stderr}")
        return res

    def test_selfcheck_matches_fixture(self):
        """--selfcheck with the test salt produces the checked-in expected JSON."""
        res = self._run("--selfcheck")
        produced = json.loads(res.stdout)
        with EXPECTED_SELFCHECK.open() as f:
            expected = json.load(f)
        self.assertEqual(
            produced, expected,
            "selfcheck output diverged from tests/fixtures/selfcheck.expected.json. "
            "If this was intentional, regenerate: "
            f"TOKENMIN_SALT_PATH={TEST_SALT} {TOKENMIN_BIN} --selfcheck > {EXPECTED_SELFCHECK}"
        )

    def test_no_anonymize_requires_two_flags(self):
        """--no-anonymize without --i-know-what-im-doing is refused."""
        res = self._run(
            "--no-anonymize", "--days", "1", "--snapshot", "/tmp/refused.json",
            expect_ok=False,
        )
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("--i-know-what-im-doing", res.stderr)
        self.assertFalse(Path("/tmp/refused.json").exists())

    def test_submit_http_non_localhost_refused(self):
        """--submit-url over plain http to a non-localhost host is refused."""
        res = self._run(
            "--submit-url", "http://example.com/analyze", "--days", "1",
            expect_ok=False,
        )
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("HTTPS required", res.stderr)

    def test_no_anonymize_with_submit_refused(self):
        """--no-anonymize + --submit-url is refused regardless of the second flag."""
        res = self._run(
            "--no-anonymize", "--i-know-what-im-doing",
            "--submit-url", "https://example.com/analyze",
            expect_ok=False,
        )
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("refusing to submit", res.stderr)

    def test_snapshot_refuses_overwrite_without_force(self):
        with tempfile.TemporaryDirectory() as td:
            snap = Path(td) / "snap.json"
            snap.write_text("placeholder")
            # Need a real source — point at a tmp dir that looks like ~/.claude.
            claude = Path(td) / "claude"
            (claude / "projects").mkdir(parents=True)
            (claude / "settings.json").write_text("{}")
            res = self._run(
                "--claude-home", str(claude),
                "--snapshot", str(snap),
                "--days", "1",
                expect_ok=False,
            )
            self.assertNotEqual(res.returncode, 0)
            self.assertIn("already exists", res.stderr)
            # File still has its placeholder content.
            self.assertEqual(snap.read_text(), "placeholder")

    def test_snapshot_file_mode_0600(self):
        with tempfile.TemporaryDirectory() as td:
            snap = Path(td) / "snap.json"
            claude = Path(td) / "claude"
            (claude / "projects").mkdir(parents=True)
            (claude / "settings.json").write_text("{}")
            self._run(
                "--claude-home", str(claude),
                "--snapshot", str(snap),
                "--days", "1",
            )
            self.assertTrue(snap.exists())
            mode = snap.stat().st_mode & 0o777
            self.assertEqual(mode, 0o600, f"snapshot mode is {oct(mode)}, expected 0o600")


if __name__ == "__main__":
    unittest.main(verbosity=2)
