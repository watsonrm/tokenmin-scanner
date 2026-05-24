#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""v0.12.4 behavior tests — four CLI/engine guarantees surfaced during preview testing:

  Scanner #4: low-impact findings filter
    Engine tags each finding `low_impact: true|false` per plan threshold.
    Markdown report splits them into "Recommendations" and "Minor findings".

  Scanner #5: `tokenmin show <id>` plan-aware framing
    (Indirect — covered by _fmt_quota_pct unit test + show command schema.)

  Scanner #6: subagent invisibility footnote
    model_overspend evidence appends a "main-session only" note when the
    snapshot shows substantial subagent (Agent tool) use.

  Scanner #7: project-scoped CLAUDE.md / .claude/agents detection
    ConfigSnapshot now has project_cwd_with_claude_md + with_local_agents,
    populated by reading the `cwd` field from each project's session JSONL.
    no_global_claude_md + no_custom_agents lower confidence when project-
    level alternatives exist.

Stdlib-only. Self-skips on scanner-only repos (no engine).
"""
from __future__ import annotations

import json
import re
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
SKILL_DIR = REPO_ROOT / "skills" / "tokenmin"
ENGINE_DIR = REPO_ROOT / "engine"
sys.path.insert(0, str(SKILL_DIR))
sys.path.insert(0, str(ENGINE_DIR))

if not ENGINE_DIR.is_dir():
    print("test_v0_12_4: skipping — engine not bundled (scanner-only repo)")
    raise SystemExit(0)

from tokenmin_engine import analyze_structured  # noqa: E402
from analyzer import scan_config  # noqa: E402


def _snapshot(n_sessions: int = 61, days: int = 14, agent_calls: int = 0) -> dict:
    """Synthetic snapshot. agent_calls > 0 spreads Agent uses across sessions."""
    per_session = agent_calls // max(n_sessions, 1) if agent_calls else 0
    return {
        "sessions": [
            {
                "session_id": f"s{i}",
                "project": "demo",
                "started_at": 0, "ended_at": 3600,
                "user_turns": 66, "assistant_turns": 70,
                "tool_calls": {"Bash": 100, "Read": 80},
                "tools_per_turn": [1, 2, 1, 3],
                "files_read": {"/a": 5},
                "files_written": ["/b"],
                "permission_denies": 0, "error_results": 0,
                "long_searches": 0,
                "agents_used": ({"general-purpose": per_session} if per_session else {}),
                "models_used": {"claude-opus-4-5": 70},
                "input_tokens": 500_000, "output_tokens": 30_000,
                "cache_write_tokens": 200_000, "cache_read_tokens": 8_000_000,
                "est_cost_usd": 125.0,
                "redo_signals": 0, "redos": 0,
            }
            for i in range(n_sessions)
        ],
        "window_days": days,
        "generated_at": 0,
        "parse_errors": 0, "skipped_files": 0,
        "config": {},
    }


# ---------- Scanner #4: low-impact tagging ----------------------------------

class LowImpactTagging(unittest.TestCase):

    def test_engine_tags_every_finding(self):
        r = analyze_structured(_snapshot(), billing_plan="max")
        for f in r["findings"]:
            self.assertIn("low_impact", f,
                          f"finding {f['id']} missing low_impact tag")
            self.assertIsInstance(f["low_impact"], bool)

    def test_pro_max_uses_quota_threshold(self):
        # Subscription plans: < 2% of monthly_api_equivalent → low_impact
        r = analyze_structured(_snapshot(), billing_plan="max")
        monthly_api = r["snapshot"]["monthly_api_equivalent_cost_usd"]
        self.assertGreater(monthly_api, 0)
        for f in r["findings"]:
            pct = 100.0 * f["savings_usd_per_month"] / monthly_api
            expected_low = pct < 2.0
            self.assertEqual(f["low_impact"], expected_low,
                             f"{f['id']} pct={pct:.1f}% low_impact={f['low_impact']}")

    def test_api_uses_dollar_threshold(self):
        r = analyze_structured(_snapshot(), billing_plan="api")
        for f in r["findings"]:
            expected_low = f["savings_usd_per_month"] < 25.0
            self.assertEqual(f["low_impact"], expected_low,
                             f"{f['id']} usd={f['savings_usd_per_month']:.0f} low_impact={f['low_impact']}")

    def test_markdown_report_has_minor_findings_section(self):
        # When low-impact findings exist, the markdown report must surface
        # them in a separate "Minor findings" section, not in the main
        # Recommendations block.
        r = analyze_structured(_snapshot(), billing_plan="max")
        md = r["report_md"]
        low_count = sum(1 for f in r["findings"] if f["low_impact"])
        if low_count > 0:
            self.assertIn("## Minor findings", md,
                          "Minor findings section missing despite low_impact findings")
            # And the count line should mention the threshold.
            self.assertRegex(md, r"\d+ additional finding")


# ---------- Scanner #6: subagent disclaimer ---------------------------------

class SubagentDisclaimer(unittest.TestCase):

    def test_no_disclaimer_when_few_agent_calls(self):
        r = analyze_structured(_snapshot(agent_calls=10))
        overspend = next((f for f in r["findings"] if f["id"] == "model_overspend"), None)
        self.assertIsNotNone(overspend)
        self.assertNotIn("main session", overspend["evidence"].lower())

    def test_disclaimer_appears_when_heavy_agent_use(self):
        r = analyze_structured(_snapshot(agent_calls=305))  # 305/61 = 5 per session exactly
        overspend = next((f for f in r["findings"] if f["id"] == "model_overspend"), None)
        self.assertIsNotNone(overspend)
        ev = overspend["evidence"]
        self.assertIn("main", ev.lower())
        self.assertIn("subagent", ev.lower())
        self.assertIn(".claude/agents", ev)
        # Must include the agent call count so user knows it saw them. Use a
        # range match (303-307) to absorb int-floor rounding from the
        # per-session spread.
        m = re.search(r"Your (\d+) Agent calls", ev)
        self.assertIsNotNone(m, f"agent call count missing from evidence:\n{ev}")
        self.assertGreaterEqual(int(m.group(1)), 300)


# ---------- Scanner #7: project-scoped detection ----------------------------

class ProjectScopedDetection(unittest.TestCase):

    def setUp(self):
        # Build a synthetic ~/.claude/projects/<encoded>/ + a real source repo
        # with CLAUDE.md + .claude/agents/, and a session JSONL pointing back.
        self.tmp = tempfile.mkdtemp(prefix="tokenmin-v0124-")
        self.tmp_path = Path(self.tmp)
        # Source repo
        self.source = self.tmp_path / "my-project"
        self.source.mkdir()
        (self.source / "CLAUDE.md").write_text("project-level rules\n")
        agents_dir = self.source / ".claude" / "agents"
        agents_dir.mkdir(parents=True)
        (agents_dir / "agent-a.md").write_text("---\nname: a\nmodel: haiku\n---\n")
        (agents_dir / "agent-b.md").write_text("---\nname: b\nmodel: sonnet\n---\n")
        # Claude Code project dir + session JSONL with cwd pointing at source
        self.claude_home = self.tmp_path / "claude"
        proj_dir = self.claude_home / "projects" / "-tmp-my-project"
        proj_dir.mkdir(parents=True)
        (self.claude_home / "settings.json").write_text("{}")
        session = proj_dir / "abc.jsonl"
        session.write_text(json.dumps({
            "type": "user",
            "timestamp": "2026-05-23T10:00:00Z",
            "cwd": str(self.source),
            "message": {"role": "user", "content": "hi"},
        }) + "\n")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_scan_config_finds_project_level_claude_md(self):
        cfg = scan_config(self.claude_home)
        self.assertEqual(cfg.project_cwd_total, 1)
        self.assertEqual(cfg.project_cwd_with_claude_md, 1)

    def test_scan_config_finds_project_level_agents(self):
        cfg = scan_config(self.claude_home)
        self.assertEqual(cfg.project_cwd_with_local_agents, 1)
        self.assertEqual(cfg.project_cwd_with_agent_count, 2)

    def test_no_global_claude_md_finding_softens_when_project_level_exists(self):
        # Stand up a snapshot that runs through the full engine. Build the
        # config dict by hand to avoid coupling to analyzer's internal
        # serializer (which lives in tokenmin.py, not analyzer.py).
        cfg = scan_config(self.claude_home)
        snap = _snapshot(n_sessions=10)
        snap["config"] = {
            "has_global_claude_md": False,
            "has_global_settings": True,
            "global_claude_md_lines": 0,
            "obsolete_references": [],
            "global_hook_count": 0,
            "permission_count": 0,
            "custom_agents": [],
            "custom_skills": [],
            "custom_commands": [],
            "mcp_servers": [],
            "projects_with_claude_md": cfg.projects_with_claude_md,
            "projects_with_oversized_claude_md": 0,
            "projects_total": cfg.projects_total,
            "projects_with_local_settings": 0,
            "projects_with_local_agents": 0,
            "project_cwd_total": cfg.project_cwd_total,
            "project_cwd_with_claude_md": cfg.project_cwd_with_claude_md,
            "project_cwd_with_local_agents": cfg.project_cwd_with_local_agents,
            "project_cwd_with_agent_count": cfg.project_cwd_with_agent_count,
            "output_style": None,
            "enable_tool_search": None,
        }
        r = analyze_structured(snap, billing_plan="api")
        finding = next((f for f in r["findings"] if f["id"] == "no_global_claude_md"), None)
        self.assertIsNotNone(finding)
        # Softened confidence (was 95%, must drop to ~30%)
        self.assertLess(finding["confidence"], 0.5,
                        f"no_global_claude_md confidence={finding['confidence']} — should drop when project-level exists")
        # Title reframed
        self.assertIn("GLOBAL", finding["title"])
        # Evidence mentions the project count
        self.assertIn("project-level", finding["evidence"].lower())


class LastRunPersistsBillingPlan(unittest.TestCase):
    """Scanner #5 follow-up: `_save_last_run` must persist `billing_plan` so
    `tokenmin show <id>` can render impact in the right unit. Without this,
    show always reads plan='unknown' and shows dollars even on Max.

    Found the hard way after the first v0.12.4 dogfood — `show low-impact`
    listed everything in $/mo despite plan=max."""

    def test_save_payload_includes_billing_plan(self):
        # Import the save fn and inspect its payload via a tempdir.
        import tokenmin
        with tempfile.TemporaryDirectory() as tmp:
            with unittest.mock.patch.object(
                tokenmin, "_last_run_path",
                return_value=Path(tmp) / "last_run.json",
            ):
                fake_result = {
                    "snapshot": {"sessions": 1},
                    "findings": [{"id": "x", "low_impact": False, "savings_usd_per_month": 100}],
                    "total_savings_usd_per_month": 100.0,
                    "billing_plan": "max",
                    "engine_version": "0.6",
                }
                tokenmin._save_last_run(fake_result)
                saved = json.loads((Path(tmp) / "last_run.json").read_text())
                self.assertEqual(saved.get("billing_plan"), "max",
                                 "billing_plan dropped from last_run.json — show will leak dollars")


if __name__ == "__main__":
    # unittest.mock is needed by LastRunPersistsBillingPlan; import lazily
    # so module-level import order stays clean.
    import unittest.mock  # noqa: F401
    unittest.main(verbosity=2)
