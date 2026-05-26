#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""v0.12.6 detector tests — 11 new patterns from scanner#15.

Each detector has at least one positive test (it fires) and one negative test
(it correctly stays silent on a non-pattern input). Where the detector has
non-trivial thresholds (e.g., needs >=N events), the tests exercise the
boundary.

Stdlib-only. Self-skips on scanner-only repos.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from collections import Counter
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
SKILL_DIR = REPO_ROOT / "skills" / "tokenmin"
ENGINE_DIR = REPO_ROOT / "engine"
sys.path.insert(0, str(SKILL_DIR))
sys.path.insert(0, str(ENGINE_DIR))

if not ENGINE_DIR.is_dir():
    print("test_v0_12_6: skipping — engine not bundled (scanner-only repo)")
    raise SystemExit(0)

from tokenmin_engine import analyze_structured  # noqa: E402


def _base_snapshot(n_sessions: int = 20, days: int = 14, **session_overrides) -> dict:
    """Build a synthetic snapshot with v0.12.6 fields. Pass kwargs to override
    individual SessionStats fields uniformly across all sessions."""
    base = {
        "session_id": "s",
        "project": "demo",
        "started_at": 0,
        "ended_at": 3600,
        "user_turns": 20,
        "assistant_turns": 20,
        "tool_calls": {},
        "tools_per_turn": [1] * 20,
        "files_read": {},
        "files_written": [],
        "permission_denies": 0,
        "error_results": 0,
        "long_searches": 0,
        "agents_used": {},
        "models_used": {"claude-sonnet-4-6": 20},
        "input_tokens": 100_000,
        "output_tokens": 5_000,
        "cache_write_tokens": 30_000,
        "cache_read_tokens": 200_000,
        "est_cost_usd": 0.5,
        "redo_signals": 0,
        # v0.12.6 fields:
        "bash_file_ops": 0,
        "cache_thrash_events": 0,
        "thinking_bloat_turns": 0,
        "hook_event_chars": 0,
        "hook_event_fires": 0,
        "denied_patterns": {},
        "compacts": 0,
        "compact_then_died": 0,
        "opus_compactions": 0,
    }
    base.update(session_overrides)
    return {
        "sessions": [{**base, "session_id": f"s{i}"} for i in range(n_sessions)],
        "window_days": days,
        "generated_at": 0,
        "parse_errors": 0,
        "skipped_files": 0,
        "config": {},
    }


def _ids(findings: list) -> set:
    return {f["id"] for f in findings}


class MCPZombieServers(unittest.TestCase):
    def test_fires_when_server_configured_but_never_invoked(self):
        snap = _base_snapshot(n_sessions=20, tool_calls={"Bash": 5, "Read": 3})
        snap["config"] = {"mcp_servers": ["github", "slack"]}
        r = analyze_structured(snap)
        self.assertIn("mcp_zombie_servers", _ids(r["findings"]))

    def test_silent_when_server_actually_used(self):
        snap = _base_snapshot(n_sessions=20, tool_calls={"mcp__github__get_pr": 5})
        snap["config"] = {"mcp_servers": ["github"]}
        r = analyze_structured(snap)
        self.assertNotIn("mcp_zombie_servers", _ids(r["findings"]))

    def test_silent_when_no_servers_configured(self):
        snap = _base_snapshot(n_sessions=20)
        snap["config"] = {"mcp_servers": []}
        r = analyze_structured(snap)
        self.assertNotIn("mcp_zombie_servers", _ids(r["findings"]))

    def test_silent_when_only_desktop_servers_configured(self):
        # Regression for the 2026-05-25 qmd false-positive incident.
        # Same-surface rule (DETECTOR_RULES.md): Desktop-only servers must
        # NOT be flagged against Code session evidence. Code never loaded
        # their schema, so there's nothing to recover.
        snap = _base_snapshot(n_sessions=20, tool_calls={"Bash": 5})
        snap["config"] = {
            "mcp_servers": [],
            "mcp_servers_desktop_only": ["qmd"],
        }
        r = analyze_structured(snap)
        self.assertNotIn("mcp_zombie_servers", _ids(r["findings"]),
                         "Desktop-only servers must not be judged against Code session data")

    def test_fires_on_code_server_when_desktop_also_present(self):
        # Symmetry check: a legit Code zombie should still fire even when a
        # separate Desktop-only entry exists. The fix must not over-suppress.
        snap = _base_snapshot(n_sessions=20, tool_calls={"Bash": 5})
        snap["config"] = {
            "mcp_servers": ["github"],
            "mcp_servers_desktop_only": ["qmd"],
        }
        r = analyze_structured(snap)
        self.assertIn("mcp_zombie_servers", _ids(r["findings"]),
                      "Code-loaded zombie must still fire even with Desktop entries present")


class ParallelToolsUnderused(unittest.TestCase):
    def test_fires_on_sequential_pattern(self):
        # Sessions with 12 Read calls but tools_per_turn all 1.
        snap = _base_snapshot(
            n_sessions=10,
            tool_calls={"Read": 12, "Grep": 0, "Glob": 0, "Bash": 0},
            tools_per_turn=[1] * 12,
        )
        r = analyze_structured(snap)
        self.assertIn("parallel_tools_underused", _ids(r["findings"]))

    def test_silent_when_already_parallel(self):
        # Mean tools-per-turn well above 1.3 → parallelism is happening.
        snap = _base_snapshot(
            n_sessions=10,
            tool_calls={"Read": 12},
            tools_per_turn=[3, 4, 3, 2],
        )
        r = analyze_structured(snap)
        self.assertNotIn("parallel_tools_underused", _ids(r["findings"]))


class OpusForSubagents(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.tmp_path = Path(self.tmp)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_fires_when_subagent_inherits_from_opus(self):
        agent = self.tmp_path / "researcher.md"
        agent.write_text(
            "---\nname: researcher\ndescription: hunts for files\ntools: Read, Grep\n---\n# Researcher\n"
        )
        snap = _base_snapshot(
            n_sessions=10,
            agents_used={"researcher": 5},
            models_used={"claude-opus-4-7": 20},
        )
        snap["config"] = {"custom_agents": [str(agent)]}
        r = analyze_structured(snap)
        self.assertIn("opus_for_subagents", _ids(r["findings"]))

    def test_silent_when_subagent_has_explicit_model(self):
        agent = self.tmp_path / "researcher.md"
        agent.write_text(
            "---\nname: researcher\nmodel: haiku\ntools: Read, Grep\n---\n# Researcher\n"
        )
        snap = _base_snapshot(
            n_sessions=10,
            agents_used={"researcher": 5},
            models_used={"claude-opus-4-7": 20},
        )
        snap["config"] = {"custom_agents": [str(agent)]}
        r = analyze_structured(snap)
        self.assertNotIn("opus_for_subagents", _ids(r["findings"]))


class SubagentAvoidanceOnHugeContext(unittest.TestCase):
    def test_fires_on_heavy_investigation_in_main_thread(self):
        snap = _base_snapshot(
            n_sessions=10,
            tool_calls={"Read": 10, "Grep": 5, "Glob": 3},
            agents_used={},
            input_tokens=2_000_000,  # 2M / 20 turns = 100K per turn
            assistant_turns=20,
        )
        r = analyze_structured(snap)
        self.assertIn("subagent_avoidance_on_huge_context", _ids(r["findings"]))

    def test_silent_when_subagents_are_used(self):
        snap = _base_snapshot(
            n_sessions=10,
            tool_calls={"Read": 10, "Grep": 5, "Glob": 3},
            agents_used={"researcher": 8},
            input_tokens=2_000_000,
        )
        r = analyze_structured(snap)
        self.assertNotIn("subagent_avoidance_on_huge_context", _ids(r["findings"]))


class ToolSearchOffWithManyServers(unittest.TestCase):
    def test_fires_with_many_servers_no_tool_search(self):
        snap = _base_snapshot(n_sessions=10)
        snap["config"] = {
            "mcp_servers": ["github", "slack", "drive", "pipedrive", "asana"],
            "enable_tool_search": None,
        }
        r = analyze_structured(snap)
        self.assertIn("tool_search_off_with_many_servers", _ids(r["findings"]))

    def test_silent_when_tool_search_enabled(self):
        snap = _base_snapshot(n_sessions=10)
        snap["config"] = {
            "mcp_servers": ["github", "slack", "drive", "pipedrive", "asana"],
            "enable_tool_search": "auto",
        }
        r = analyze_structured(snap)
        self.assertNotIn("tool_search_off_with_many_servers", _ids(r["findings"]))


class BashCatInsteadOfRead(unittest.TestCase):
    def test_fires_when_bash_dominates_file_ops(self):
        # 20 bash file ops, 5 Read calls → ratio = 20/25 = 80%, well over 15%.
        snap = _base_snapshot(
            n_sessions=10,
            bash_file_ops=20,
            tool_calls={"Read": 5, "Bash": 20},
        )
        r = analyze_structured(snap)
        self.assertIn("bash_cat_instead_of_read", _ids(r["findings"]))

    def test_silent_when_dedicated_tools_dominate(self):
        # 2 bash file ops vs 50 Read calls → 4%, under threshold.
        snap = _base_snapshot(
            n_sessions=10,
            bash_file_ops=2,
            tool_calls={"Read": 50, "Bash": 2},
        )
        r = analyze_structured(snap)
        self.assertNotIn("bash_cat_instead_of_read", _ids(r["findings"]))


class CacheThrashShortGaps(unittest.TestCase):
    def test_fires_on_substantial_thrash(self):
        snap = _base_snapshot(
            n_sessions=10,
            cache_thrash_events=5,  # 50 across 10 sessions, scaled monthly ~107
            cache_write_tokens=500_000,
        )
        r = analyze_structured(snap)
        self.assertIn("cache_thrash_short_gaps", _ids(r["findings"]))

    def test_silent_on_minimal_thrash(self):
        snap = _base_snapshot(n_sessions=10, cache_thrash_events=0)
        r = analyze_structured(snap)
        self.assertNotIn("cache_thrash_short_gaps", _ids(r["findings"]))


class EffortHighForTrivialWork(unittest.TestCase):
    def test_fires_when_thinking_bloat_ratio_high(self):
        # 5 bloat turns per session, 20 turns per session → 25% ratio.
        snap = _base_snapshot(
            n_sessions=10,
            thinking_bloat_turns=5,
            assistant_turns=20,
        )
        r = analyze_structured(snap)
        self.assertIn("effort_high_for_trivial_work", _ids(r["findings"]))

    def test_silent_when_no_thinking_bloat(self):
        snap = _base_snapshot(n_sessions=10, thinking_bloat_turns=0)
        r = analyze_structured(snap)
        self.assertNotIn("effort_high_for_trivial_work", _ids(r["findings"]))


class HookTokenBurner(unittest.TestCase):
    def test_fires_when_hook_output_chars_large(self):
        # 3 hook fires × 3000 chars per session = 9K chars/session; 10 sessions
        # = 30 fires × 30K total chars → avg 1000/fire. Under threshold of 2000.
        # Increase per-fire chars to clear the bar.
        snap = _base_snapshot(
            n_sessions=10,
            hook_event_fires=3,  # 30 total fires
            hook_event_chars=15_000,  # 150K total → avg 5000/fire
        )
        r = analyze_structured(snap)
        self.assertIn("hook_token_burner", _ids(r["findings"]))

    def test_silent_on_modest_hook_output(self):
        snap = _base_snapshot(
            n_sessions=10,
            hook_event_fires=3,
            hook_event_chars=1_000,  # avg 333 chars/fire
        )
        r = analyze_structured(snap)
        self.assertNotIn("hook_token_burner", _ids(r["findings"]))

    def test_confidence_is_low_acknowledging_heuristic(self):
        snap = _base_snapshot(
            n_sessions=10,
            hook_event_fires=3,
            hook_event_chars=15_000,
        )
        r = analyze_structured(snap)
        for f in r["findings"]:
            if f["id"] == "hook_token_burner":
                self.assertLessEqual(f["confidence"], 0.5,
                    "hook_token_burner must signal its heuristic nature with confidence <= 0.5")
                break
        else:
            self.fail("hook_token_burner should have fired")


class PermissionDeniesLoop(unittest.TestCase):
    def test_fires_when_same_pattern_denied_5plus_times(self):
        snap = _base_snapshot(
            n_sessions=10,
            denied_patterns={"Bash: rm": 8, "Write: /etc/passwd": 5},
            permission_denies=13,
        )
        r = analyze_structured(snap)
        self.assertIn("permission_denies_loop", _ids(r["findings"]))

    def test_silent_when_denials_are_one_offs(self):
        # Each pattern hit only once across the whole window — no loop.
        # Use a small session count + tiny per-session counts so the
        # cross-session aggregate stays below the 5-deny threshold.
        snap = _base_snapshot(
            n_sessions=2,
            denied_patterns={"Bash: foo": 1, "Bash: bar": 1, "Write: /tmp": 1},
            permission_denies=3,
        )
        r = analyze_structured(snap)
        self.assertNotIn("permission_denies_loop", _ids(r["findings"]))


class OpusForCompaction(unittest.TestCase):
    def test_fires_when_compaction_ran_on_opus(self):
        snap = _base_snapshot(n_sessions=10, opus_compactions=2)
        r = analyze_structured(snap)
        self.assertIn("opus_for_compaction", _ids(r["findings"]))

    def test_silent_when_no_opus_compactions(self):
        snap = _base_snapshot(n_sessions=10, opus_compactions=0)
        r = analyze_structured(snap)
        self.assertNotIn("opus_for_compaction", _ids(r["findings"]))


class AllElevenRegistered(unittest.TestCase):
    """Sanity guard: every v0.12.6 detector must be in run_all's registry.
    Catches the regression where a detector function is defined but forgotten
    in the DETECTORS list."""

    def test_all_11_v0126_detectors_in_registry(self):
        import patterns
        registered = {d.__name__ for d in patterns.DETECTORS}
        expected = {
            "detect_mcp_zombie_servers",
            "detect_parallel_tools_underused",
            "detect_opus_for_subagents",
            "detect_subagent_avoidance_on_huge_context",
            "detect_tool_search_off_with_many_servers",
            "detect_bash_cat_instead_of_read",
            "detect_cache_thrash_short_gaps",
            "detect_effort_high_for_trivial_work",
            "detect_hook_token_burner",
            "detect_permission_denies_loop",
            "detect_opus_for_compaction",
        }
        missing = expected - registered
        self.assertFalse(missing, f"v0.12.6 detectors missing from registry: {missing}")


class ModelOverspendContextAware(unittest.TestCase):
    """v0.12.8 (scanner#9): model_overspend must downgrade confidence when the
    user has clearly done subagent routing already. Otherwise the recommendation
    to switch to Sonnet fires at $$$$ critical for users whose subagent layer
    is already carrying the cheap-tier work — wrong by default."""

    @staticmethod
    def _opus_heavy(agent_calls: int) -> dict:
        per_session = agent_calls // 20 if agent_calls else 0
        return _base_snapshot(
            n_sessions=20,
            tool_calls={"Bash": 5},
            agents_used=({"researcher": per_session} if per_session else {}),
            models_used={"claude-opus-4-7": 30},
            assistant_turns=30,
            user_turns=30,
            input_tokens=200_000,
            cache_write_tokens=50_000,
            cache_read_tokens=500_000,
            est_cost_usd=50.0,
        )

    def test_downgrades_confidence_when_subagent_routing_is_heavy(self):
        # 300 / 20 = 15 calls per session → 300 total > 100 threshold
        snap = self._opus_heavy(agent_calls=300)
        r = analyze_structured(snap)
        overspend = next((f for f in r["findings"] if f["id"] == "model_overspend"), None)
        self.assertIsNotNone(overspend, "model_overspend should still fire (informational)")
        self.assertLessEqual(overspend["confidence"], 0.25,
            "with heavy subagent volume, model_overspend confidence must drop "
            "(otherwise it ranks #1 for users who already did the routing work)")
        self.assertIn("subagent", overspend["title"].lower())

    def test_keeps_full_confidence_without_subagent_routing(self):
        snap = self._opus_heavy(agent_calls=0)
        r = analyze_structured(snap)
        overspend = next((f for f in r["findings"] if f["id"] == "model_overspend"), None)
        self.assertIsNotNone(overspend)
        self.assertGreaterEqual(overspend["confidence"], 0.5,
            "without subagent activity, model_overspend should fire at full confidence")


class WellRoutedSubagents(unittest.TestCase):
    """v0.12.8: positive informational finding that fires when the user has
    clearly done subagent routing. Counterbalances model_overspend."""

    def test_fires_with_heavy_agent_volume_and_custom_agents(self):
        import shutil
        tmp = tempfile.mkdtemp()
        try:
            agent_file = Path(tmp) / "researcher.md"
            agent_file.write_text("---\nname: r\nmodel: haiku\n---\n")
            snap = ModelOverspendContextAware._opus_heavy(300)
            snap["config"] = {"custom_agents": [str(agent_file)]}
            r = analyze_structured(snap)
            wrs = next((f for f in r["findings"] if f["id"] == "well_routed_subagents"), None)
            self.assertIsNotNone(wrs,
                "well_routed_subagents should fire when subagent routing is clearly intentional")
            self.assertEqual(wrs["savings_usd_per_month"], 0.0,
                "informational finding has zero dollar savings")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_silent_without_subagent_activity(self):
        snap = ModelOverspendContextAware._opus_heavy(10)
        r = analyze_structured(snap)
        self.assertNotIn("well_routed_subagents", _ids(r["findings"]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
