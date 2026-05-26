#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Tests for v0.12.5's update UX — `tokenmin update`, `--version` "behind"
hint, and `tokenmin doctor` update-status line.

The current UX gap Rick caught:
  - 24h cooldown means users sit a day behind without knowing
  - `--version` only printed local SHA — no "you're behind by N" hint
  - No `tokenmin update` command (the stale-pricing message lied)
  - `doctor` showed the mode but not actual status

v0.12.5 ships:
  - `_update_status()` — cached-1h check against origin/main
  - `tokenmin update` — explicit, bypasses cooldown, dirty-tree-safe
  - `--version` surfaces "update available: vX.Y.Z" when behind
  - `doctor` adds `update status:` line

These tests cover the contract without hitting the real network (subprocess
calls are mocked so CI is deterministic + offline-safe).
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from datetime import datetime, timedelta, timezone
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
SKILL_DIR = REPO_ROOT / "skills" / "tokenmin"
ENGINE_DIR = REPO_ROOT / "engine"
sys.path.insert(0, str(SKILL_DIR))
sys.path.insert(0, str(ENGINE_DIR))

import tokenmin  # noqa: E402


class UpdateStatusContract(unittest.TestCase):
    """`_update_status()` returns a stable schema, never raises."""

    def test_returns_required_keys(self):
        with unittest.mock.patch.object(
            tokenmin, "_install_dir",
            return_value=Path(tempfile.mkdtemp()),
        ):
            s = tokenmin._update_status(force_refresh=True, timeout_sec=1)
        for key in ("current_version", "current_sha", "latest_version",
                    "latest_sha", "up_to_date", "checked_at", "error"):
            self.assertIn(key, s)

    def test_handles_non_git_install_gracefully(self):
        with tempfile.TemporaryDirectory() as tmp:
            with unittest.mock.patch.object(
                tokenmin, "_install_dir",
                return_value=Path(tmp),
            ):
                s = tokenmin._update_status(force_refresh=True, timeout_sec=1)
            self.assertIn("not a git repo", s["error"])
            self.assertIsNone(s["up_to_date"])

    def test_caches_within_1h_skips_ls_remote(self):
        # Pre-seed a cache file with a recent timestamp; confirm we skip the
        # network call (the *latest_* fields come from cache; current_* always
        # comes fresh from _version_info — that's the v0.12.5 fix for "cache
        # reports stale 'behind' after an update").
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".git").mkdir()
            (root / ".update-status").write_text(json.dumps({
                "current_version": "9.9.9",
                "current_sha": "abc1234abcd",
                "latest_version": "9.9.9",
                "latest_sha": "abc1234abcd",
                "up_to_date": True,
                "checked_at": datetime.now(timezone.utc).isoformat(),
                "error": "",
            }))
            ls_remote_called = []
            real_run = subprocess.run
            def watching_run(cmd, *a, **kw):
                if isinstance(cmd, list) and "ls-remote" in cmd:
                    ls_remote_called.append(cmd)
                return real_run(cmd, *a, **kw)
            with unittest.mock.patch.object(tokenmin, "_install_dir",
                                            return_value=root), \
                 unittest.mock.patch.object(tokenmin, "_version_info",
                                            return_value={"version": "9.9.9", "commit": "abc1234abcd"}), \
                 unittest.mock.patch("subprocess.run", side_effect=watching_run):
                s = tokenmin._update_status(force_refresh=False)
            self.assertEqual(s["current_version"], "9.9.9")
            self.assertTrue(s["up_to_date"])
            self.assertEqual(ls_remote_called, [],
                             "cache hit must skip the network ls-remote call")

    def test_sha_comparison_uses_prefix(self):
        # Regression for the v0.12.5 dogfood bug: _version_info() returns a
        # 12-char short SHA; `git ls-remote` returns 40-char full. Without
        # prefix compare, up_to_date is always False — update happily "pulls"
        # the same commit and --version reports "behind" forever.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".git").mkdir()
            (root / "VERSION").write_text("0.12.5\n")
            short_sha = "0e2c15c32b26"
            full_sha  = "0e2c15c32b26abcdef0123456789012345678901"
            def fake_run(cmd, *a, **kw):
                if "ls-remote" in cmd:
                    return subprocess.CompletedProcess(cmd, 0, f"{full_sha}\trefs/heads/main\n", "")
                if "show" in cmd:
                    return subprocess.CompletedProcess(cmd, 0, "0.12.5\n", "")
                return subprocess.CompletedProcess(cmd, 0, "", "")
            with unittest.mock.patch.object(tokenmin, "_install_dir", return_value=root), \
                 unittest.mock.patch.object(tokenmin, "_version_info",
                                            return_value={"version": "0.12.5", "commit": short_sha}), \
                 unittest.mock.patch("subprocess.run", side_effect=fake_run):
                s = tokenmin._update_status(force_refresh=True)
            self.assertTrue(s["up_to_date"],
                            f"SHA prefix compare broke: current={s['current_sha']!r} latest={s['latest_sha']!r}")

    def test_cache_recomputes_up_to_date_after_local_update(self):
        # Pre-seed a cache that says "behind" (current=oldsha, latest=newsha),
        # then re-read with current bumped to newsha (simulating an update).
        # The returned status must NOT report "behind" anymore — the cache
        # should refresh current_* and recompute up_to_date.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".git").mkdir()
            (root / ".update-status").write_text(json.dumps({
                "current_version": "0.12.4", "current_sha": "oldsha000000",
                "latest_version": "0.12.5", "latest_sha": "newsha000000ffffffff",
                "up_to_date": False,
                "checked_at": datetime.now(timezone.utc).isoformat(),
                "error": "",
            }))
            with unittest.mock.patch.object(tokenmin, "_install_dir", return_value=root), \
                 unittest.mock.patch.object(tokenmin, "_version_info",
                                            return_value={"version": "0.12.5", "commit": "newsha000000"}):
                s = tokenmin._update_status(force_refresh=False)
            self.assertTrue(s["up_to_date"], "cache failed to refresh current_sha after a local update")
            self.assertEqual(s["current_version"], "0.12.5")

    def test_force_refresh_bypasses_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".git").mkdir()
            (root / ".update-status").write_text(json.dumps({
                "current_version": "stale", "current_sha": "old",
                "latest_version": "stale", "latest_sha": "old",
                "up_to_date": True,
                "checked_at": datetime.now(timezone.utc).isoformat(),
                "error": "",
            }))
            # Simulate ls-remote returning a brand new sha.
            def fake_run(cmd, *a, **kw):
                if "ls-remote" in cmd:
                    return subprocess.CompletedProcess(cmd, 0, "newsha1234567 refs/heads/main\n", "")
                if "show" in cmd:
                    return subprocess.CompletedProcess(cmd, 0, "0.99.0\n", "")
                return subprocess.CompletedProcess(cmd, 0, "", "")
            with unittest.mock.patch.object(tokenmin, "_install_dir",
                                            return_value=root), \
                 unittest.mock.patch("subprocess.run", side_effect=fake_run):
                s = tokenmin._update_status(force_refresh=True)
            self.assertEqual(s["latest_sha"], "newsha1234567")
            self.assertEqual(s["latest_version"], "0.99.0")
            self.assertFalse(s["up_to_date"])


class UpdateCommandBehavior(unittest.TestCase):
    """`tokenmin update` exit codes + safety rails."""

    def test_refuses_when_autoupdate_off(self):
        with unittest.mock.patch.dict("os.environ", {"TOKENMIN_AUTOUPDATE": "off"}), \
             unittest.mock.patch.object(sys, "stderr", new_callable=__import__("io").StringIO) as err:
            rc = tokenmin._update_cmd([])
        self.assertEqual(rc, 1)
        self.assertIn("TOKENMIN_AUTOUPDATE=off", err.getvalue())

    def test_check_flag_does_not_pull(self):
        # --check should print status but never invoke `git merge` or `reset`.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".git").mkdir()
            def fake_run(cmd, *a, **kw):
                if "ls-remote" in cmd:
                    return subprocess.CompletedProcess(cmd, 0, "newsha refs/heads/main\n", "")
                if "show" in cmd:
                    return subprocess.CompletedProcess(cmd, 0, "0.99.0\n", "")
                if "merge" in cmd or "reset" in cmd:
                    self.fail("--check must not invoke `git merge` or `git reset`")
                return subprocess.CompletedProcess(cmd, 0, "", "")
            with unittest.mock.patch.object(tokenmin, "_install_dir", return_value=root), \
                 unittest.mock.patch.object(tokenmin, "_version_info",
                                            return_value={"version": "0.1.0", "commit": "oldsha"}), \
                 unittest.mock.patch("subprocess.run", side_effect=fake_run), \
                 unittest.mock.patch.dict("os.environ",
                     # tests/run.sh exports TOKENMIN_AUTOUPDATE=off for test
                     # isolation of the bash-wrapper auto-update path. The
                     # _update_cmd path treats that as "refuse" — clear it
                     # for these tests so the update code path runs.
                     {"TOKENMIN_AUTOUPDATE": ""}, clear=False):
                rc = tokenmin._update_cmd(["--check"])
            self.assertEqual(rc, 0)

    def test_self_state_files_do_not_block_update(self):
        # Regression: prior to this fix, `tokenmin --version` wrote
        # .update-status, which then made `git status --porcelain` non-empty,
        # which then made `tokenmin update` refuse with "working tree has
        # local changes". The cli self-blocked. .update-status (and
        # .last-update-check) are tokenmin's own state files; they must
        # never be treated as user dirty edits.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".git").mkdir()
            ran = {"reset": False}
            def fake_run(cmd, *a, **kw):
                if "ls-remote" in cmd:
                    return subprocess.CompletedProcess(cmd, 0, "newsha000000000000000000000000000000000000 refs/heads/main\n", "")
                if "show" in cmd:
                    return subprocess.CompletedProcess(cmd, 0, "0.99.0\n", "")
                if "status" in cmd and "--porcelain" in cmd:
                    # Mimic the real-world post-version-check state.
                    return subprocess.CompletedProcess(cmd, 0, "?? .update-status\n", "")
                if "fetch" in cmd:
                    return subprocess.CompletedProcess(cmd, 0, "", "")
                if "reset" in cmd:
                    ran["reset"] = True
                    return subprocess.CompletedProcess(cmd, 0, "", "")
                return subprocess.CompletedProcess(cmd, 0, "", "")
            with unittest.mock.patch.object(tokenmin, "_install_dir", return_value=root), \
                 unittest.mock.patch.object(tokenmin, "_version_info",
                                            return_value={"version": "0.1.0", "commit": "oldsha000000"}), \
                 unittest.mock.patch("subprocess.run", side_effect=fake_run), \
                 unittest.mock.patch.dict("os.environ",
                     # tests/run.sh exports TOKENMIN_AUTOUPDATE=off for test
                     # isolation of the bash-wrapper auto-update path. The
                     # _update_cmd path treats that as "refuse" — clear it
                     # for these tests so the update code path runs.
                     {"TOKENMIN_AUTOUPDATE": ""}, clear=False):
                rc = tokenmin._update_cmd([])
            self.assertEqual(rc, 0, "self-state files must not block update")
            self.assertTrue(ran["reset"], "update must actually reset to origin/main")

    def test_user_edits_still_block_update(self):
        # Symmetry check: real user edits — anything other than the known
        # self-state files — must still block. We don't want this fix to
        # silently swallow legitimate local changes.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".git").mkdir()
            def fake_run(cmd, *a, **kw):
                if "ls-remote" in cmd:
                    return subprocess.CompletedProcess(cmd, 0, "newsha000000000000000000000000000000000000 refs/heads/main\n", "")
                if "show" in cmd:
                    return subprocess.CompletedProcess(cmd, 0, "0.99.0\n", "")
                if "status" in cmd and "--porcelain" in cmd:
                    return subprocess.CompletedProcess(cmd, 0, " M skills/tokenmin/tokenmin.py\n", "")
                return subprocess.CompletedProcess(cmd, 0, "", "")
            with unittest.mock.patch.object(tokenmin, "_install_dir", return_value=root), \
                 unittest.mock.patch.object(tokenmin, "_version_info",
                                            return_value={"version": "0.1.0", "commit": "oldsha000000"}), \
                 unittest.mock.patch("subprocess.run", side_effect=fake_run), \
                 unittest.mock.patch.dict("os.environ", {}, clear=False), \
                 unittest.mock.patch.object(sys, "stderr", new_callable=__import__("io").StringIO):
                rc = tokenmin._update_cmd([])
            self.assertEqual(rc, 1, "real user edits must still block update")

    def test_update_uses_fetch_reset_not_merge(self):
        # Regression: upstream releases sometimes squash-and-force-push, which
        # breaks `git merge --ff-only` on installs that pulled the pre-squash
        # commits. The install dir is a mirror by design, so the update path
        # must use `fetch + reset --hard FETCH_HEAD`, not `merge --ff-only`.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".git").mkdir()
            calls = []
            def fake_run(cmd, *a, **kw):
                calls.append(tuple(cmd) if isinstance(cmd, list) else cmd)
                if "ls-remote" in cmd:
                    return subprocess.CompletedProcess(cmd, 0, "newsha000000000000000000000000000000000000 refs/heads/main\n", "")
                if "show" in cmd:
                    return subprocess.CompletedProcess(cmd, 0, "0.99.0\n", "")
                if "status" in cmd:
                    return subprocess.CompletedProcess(cmd, 0, "", "")
                if "merge" in cmd:
                    self.fail("update must not invoke `git merge` (force-push-fragile)")
                return subprocess.CompletedProcess(cmd, 0, "", "")
            with unittest.mock.patch.object(tokenmin, "_install_dir", return_value=root), \
                 unittest.mock.patch.object(tokenmin, "_version_info",
                                            return_value={"version": "0.1.0", "commit": "oldsha000000"}), \
                 unittest.mock.patch("subprocess.run", side_effect=fake_run), \
                 unittest.mock.patch.dict("os.environ",
                     # tests/run.sh exports TOKENMIN_AUTOUPDATE=off for test
                     # isolation of the bash-wrapper auto-update path. The
                     # _update_cmd path treats that as "refuse" — clear it
                     # for these tests so the update code path runs.
                     {"TOKENMIN_AUTOUPDATE": ""}, clear=False):
                rc = tokenmin._update_cmd([])
            self.assertEqual(rc, 0)
            flat = [" ".join(c) for c in calls]
            self.assertTrue(any("fetch" in c and "origin" in c and "main" in c for c in flat),
                            "must fetch origin/main before resetting")
            self.assertTrue(any("reset" in c and "--hard" in c and "FETCH_HEAD" in c for c in flat),
                            "must reset --hard FETCH_HEAD (mirror semantics)")


if __name__ == "__main__":
    unittest.main(verbosity=2)
