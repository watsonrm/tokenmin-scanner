#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Behavior tests for `tokenmin uninstall`.

Covers the three load-bearing guarantees of v0.12.2's clean uninstall:

  1. Refuses to delete anything when run from a dev/source tree
     (`_install_dir()` resolves via `__file__`, so importing tokenmin.py
     from a checkout would otherwise rm-rf the user's working copy).
  2. One-line success output in the default quiet mode:
        tokenmin <ver> uninstalled
  3. Fully clean: removes install dir, symlink, AND the installer-added
     PATH line from the shell rc — no manual cleanup left for the user.

Stdlib-only so CI runs as a single `python3 tests/test_uninstall.py`.
"""
from __future__ import annotations

import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parent.parent
SKILL_DIR = REPO_ROOT / "skills" / "tokenmin"
ENGINE_DIR = REPO_ROOT / "engine"
sys.path.insert(0, str(SKILL_DIR))
sys.path.insert(0, str(ENGINE_DIR))

import tokenmin  # noqa: E402


# install.sh writes this exact marker before its PATH export line. Keep this
# string in sync with install.sh — the test is the contract.
INSTALLER_MARKER = "# Added by tokenmin installer on 2026-01-01T00:00:00Z"


def _seed_fake_install(home: Path, version: str = "0.12.2") -> Path:
    """Build a minimal ~/.tokenmin install layout. Returns the install dir."""
    install = home / ".tokenmin"
    install.mkdir(parents=True)
    (install / "VERSION").write_text(f"{version}\n")
    tm = install / "tokenmin"
    tm.write_text("#!/usr/bin/env python3\n")
    tm.chmod(0o755)
    # Symlink in ~/.local/bin pointing at the install
    bin_dir = home / ".local" / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "tokenmin").symlink_to(tm)
    # Marker line in zshrc, with realistic context around it
    zshrc = home / ".zshrc"
    zshrc.write_text(
        '# user stuff\nalias ll="ls -lh"\n\n'
        f'{INSTALLER_MARKER}\n'
        f'export PATH="{home}/.local/bin:$PATH"\n\n'
        'export EDITOR=vim\n'
    )
    return install


class UninstallDevTreeSafety(unittest.TestCase):
    """v0.12.2 critical fix: refuse to run from a dev clone."""

    def test_refuses_to_uninstall_dev_tree(self):
        # We're imported from REPO_ROOT (a dev tree). _install_dir() resolves
        # to REPO_ROOT, which is NOT ~/.tokenmin, so uninstall must refuse.
        self.assertFalse(tokenmin._is_real_install(tokenmin._install_dir()))
        with patch.object(sys, "stderr", new_callable=io.StringIO) as err, \
             patch.object(sys, "stdout", new_callable=io.StringIO):
            rc = tokenmin._uninstall(["--yes"])
        self.assertEqual(rc, 4, "must exit 4 when refusing dev-tree uninstall")
        self.assertIn("refusing to remove", err.getvalue())
        # And the dev tree must still be intact.
        self.assertTrue((REPO_ROOT / "skills" / "tokenmin" / "tokenmin.py").is_file())


class UninstallCleanRemoval(unittest.TestCase):
    """End-to-end uninstall against a synthesized ~/.tokenmin layout."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="tokenmin-uninstall-test-")
        self.home = Path(self.tmp)
        # Patch Path.home() AND the module-level _CANONICAL_INSTALL_DIR /
        # _install_dir so the real install lookup points at our sandbox.
        # Also stub out `claude` lookup — tests must not hit the real Claude
        # Code plugin registry on the dev machine.
        self._patches = [
            patch("tokenmin.Path.home", return_value=self.home),
            patch("tokenmin._CANONICAL_INSTALL_DIR", self.home / ".tokenmin"),
            patch("tokenmin._install_dir", return_value=self.home / ".tokenmin"),
            patch("shutil.which", return_value=None),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_one_line_success_and_full_cleanup(self):
        install = _seed_fake_install(self.home, version="0.12.2")
        with patch.object(sys, "stdout", new_callable=io.StringIO) as out, \
             patch.object(sys, "stderr", new_callable=io.StringIO):
            rc = tokenmin._uninstall(["--yes"])
        self.assertEqual(rc, 0)
        # Exactly one line, matching install greet style.
        lines = out.getvalue().rstrip("\n").split("\n")
        self.assertEqual(len(lines), 1, f"expected 1 line, got {len(lines)}: {lines}")
        self.assertEqual(lines[0], "tokenmin 0.12.2 uninstalled")
        # Everything gone:
        self.assertFalse(install.exists(), "install dir still present")
        self.assertFalse((self.home / ".local" / "bin" / "tokenmin").is_symlink(),
                         "symlink still present")
        rc_text = (self.home / ".zshrc").read_text()
        self.assertNotIn(INSTALLER_MARKER, rc_text,
                         "installer marker still in zshrc")
        self.assertNotIn(".local/bin", rc_text,
                         "PATH export line still in zshrc")
        # User's own lines preserved:
        self.assertIn("alias ll=", rc_text)
        self.assertIn("export EDITOR=vim", rc_text)

    def test_dry_run_removes_nothing(self):
        install = _seed_fake_install(self.home)
        with patch.object(sys, "stderr", new_callable=io.StringIO) as err, \
             patch.object(sys, "stdout", new_callable=io.StringIO):
            rc = tokenmin._uninstall(["--dry-run", "--yes"])
        self.assertEqual(rc, 0)
        self.assertIn("uninstall plan", err.getvalue())
        self.assertIn("dry-run", err.getvalue())
        # Nothing actually removed:
        self.assertTrue(install.exists())
        self.assertTrue((self.home / ".local" / "bin" / "tokenmin").is_symlink())
        self.assertIn(INSTALLER_MARKER, (self.home / ".zshrc").read_text())

    def test_idempotent_on_already_uninstalled(self):
        # No install present — must not error, must announce nothing-to-do.
        with patch.object(sys, "stdout", new_callable=io.StringIO) as out:
            rc = tokenmin._uninstall(["--yes"])
        self.assertEqual(rc, 0)
        self.assertIn("nothing to uninstall", out.getvalue())

    def test_strip_installer_marker_preserves_user_content(self):
        rc_file = self.home / ".zshrc"
        rc_file.write_text(
            "# my own setup\nexport PATH=/opt/bin:$PATH\n\n"
            f"{INSTALLER_MARKER}\n"
            'export PATH="$HOME/.local/bin:$PATH"\n\n'
            "# below installer\nalias g=git\n"
        )
        changed = tokenmin._strip_installer_marker(rc_file)
        self.assertTrue(changed)
        after = rc_file.read_text()
        self.assertNotIn(INSTALLER_MARKER, after)
        self.assertNotIn("$HOME/.local/bin", after)
        # User content untouched:
        self.assertIn("# my own setup", after)
        self.assertIn("export PATH=/opt/bin", after)
        self.assertIn("alias g=git", after)


if __name__ == "__main__":
    unittest.main(verbosity=2)
