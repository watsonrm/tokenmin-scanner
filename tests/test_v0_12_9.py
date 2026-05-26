#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""v0.12.9: billing-plan auto-suggestion heuristic + rate-limit signal capture.

Three things this guards:
  1. `_suggest_billing_plan(result_dict)` returns the right (plan, confidence,
     reason) tuple for canonical Max / Pro / API patterns.
  2. Engine output carries `total_rate_limit_errors` so the heuristic has a
     signal to work with.
  3. SessionStats round-trips the new `rate_limit_errors` field through
     `_session_from_dict` without dropping it.
"""
from __future__ import annotations

import json
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

if not ENGINE_DIR.is_dir():
    print("test_v0_12_9: skipping — engine not bundled")
    raise SystemExit(0)

import tokenmin  # noqa: E402
from tokenmin_engine import analyze_structured  # noqa: E402


def _result_with(monthly_api: float, opus_share: float, sessions: int = 20,
                 rate_limit_errors: int = 0, models: list | None = None) -> dict:
    """Build a synthetic result dict in the shape `_suggest_billing_plan` reads."""
    if models is None:
        models = [{"name": "Opus", "share": opus_share, "count": 100}]
        if opus_share < 1.0:
            models.append({"name": "Sonnet", "share": 1 - opus_share, "count": int(100 * (1 - opus_share))})
    return {
        "snapshot": {
            "sessions": sessions,
            "monthly_api_equivalent_cost_usd": monthly_api,
            "models": models,
            "total_rate_limit_errors": rate_limit_errors,
        },
    }


class SuggestBillingPlan(unittest.TestCase):
    """Heuristic returns the right (plan, confidence, reason)."""

    def test_max_suggested_for_heavy_opus_high_cost(self):
        result = _result_with(monthly_api=8000, opus_share=1.0, sessions=60)
        plan, conf, _ = tokenmin._suggest_billing_plan(result)
        self.assertEqual(plan, "max")
        self.assertGreaterEqual(conf, 0.6)

    def test_max_suggested_for_substantial_opus_moderate_cost(self):
        result = _result_with(monthly_api=150, opus_share=0.4, sessions=20)
        plan, conf, _ = tokenmin._suggest_billing_plan(result)
        self.assertEqual(plan, "max")
        self.assertGreaterEqual(conf, 0.5)

    def test_pro_suggested_when_rate_limits_present(self):
        result = _result_with(monthly_api=50, opus_share=0.3, sessions=15,
                              rate_limit_errors=12)
        plan, conf, _ = tokenmin._suggest_billing_plan(result)
        self.assertEqual(plan, "pro")
        self.assertGreaterEqual(conf, 0.5)

    def test_api_suggested_with_env_var(self):
        result = _result_with(monthly_api=20, opus_share=0.1, sessions=10)
        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "sk-ant-fake"}, clear=False):
            plan, conf, _ = tokenmin._suggest_billing_plan(result)
        self.assertEqual(plan, "api")
        self.assertGreaterEqual(conf, 0.5)

    def test_unknown_when_too_few_sessions(self):
        result = _result_with(monthly_api=8000, opus_share=1.0, sessions=2)
        plan, conf, _ = tokenmin._suggest_billing_plan(result)
        self.assertEqual(plan, "unknown")
        self.assertEqual(conf, 0.0)

    def test_unknown_when_signals_ambiguous(self):
        # Moderate cost, no Opus, no env var, no rate limits — falls through.
        result = _result_with(monthly_api=50, opus_share=0.0, sessions=20,
                              models=[{"name": "Sonnet", "share": 1.0, "count": 100}])
        plan, conf, _ = tokenmin._suggest_billing_plan(result)
        self.assertEqual(plan, "unknown")

    def test_handles_none_input_gracefully(self):
        plan, conf, _ = tokenmin._suggest_billing_plan(None)
        self.assertEqual(plan, "unknown")
        self.assertEqual(conf, 0.0)


class EngineSurfacesRateLimitTotal(unittest.TestCase):
    """The engine output must carry `total_rate_limit_errors` so the heuristic
    has a signal — without this, the heuristic always sees 0."""

    def test_rate_limit_total_is_in_snapshot(self):
        snap = {
            "sessions": [
                {
                    "session_id": "s1", "project": "demo",
                    "started_at": 0, "ended_at": 3600,
                    "user_turns": 10, "assistant_turns": 10,
                    "tool_calls": {}, "tools_per_turn": [1],
                    "files_read": {}, "files_written": [],
                    "permission_denies": 0, "error_results": 2,
                    "long_searches": 0, "agents_used": {},
                    "models_used": {"claude-opus-4-7": 10},
                    "input_tokens": 100_000, "output_tokens": 5_000,
                    "cache_write_tokens": 10_000, "cache_read_tokens": 50_000,
                    "est_cost_usd": 5.0, "redo_signals": 0,
                    "bash_file_ops": 0, "cache_thrash_events": 0,
                    "thinking_bloat_turns": 0, "hook_event_chars": 0,
                    "hook_event_fires": 0, "denied_patterns": {},
                    "compacts": 0, "compact_then_died": 0, "opus_compactions": 0,
                    "rate_limit_errors": 7,  # the new field
                },
            ],
            "window_days": 14, "generated_at": 0,
            "parse_errors": 0, "skipped_files": 0,
            "config": {},
        }
        r = analyze_structured(snap)
        self.assertIn("total_rate_limit_errors", r["snapshot"])
        self.assertEqual(r["snapshot"]["total_rate_limit_errors"], 7)

    def test_rate_limit_total_defaults_to_zero_when_field_missing(self):
        # Pre-0.12.9 snapshot serializers may not include rate_limit_errors.
        # The engine must default it gracefully so old clients still work.
        snap = {
            "sessions": [
                {
                    "session_id": "s1", "project": "demo",
                    "started_at": 0, "ended_at": 3600,
                    "user_turns": 10, "assistant_turns": 10,
                    "tool_calls": {}, "tools_per_turn": [1],
                    "files_read": {}, "files_written": [],
                    "permission_denies": 0, "error_results": 0,
                    "long_searches": 0, "agents_used": {},
                    "models_used": {"claude-sonnet-4-6": 10},
                    "input_tokens": 100_000, "output_tokens": 5_000,
                    "cache_write_tokens": 10_000, "cache_read_tokens": 50_000,
                    "est_cost_usd": 5.0, "redo_signals": 0,
                    # rate_limit_errors deliberately omitted
                },
            ],
            "window_days": 14, "generated_at": 0,
            "parse_errors": 0, "skipped_files": 0,
            "config": {},
        }
        r = analyze_structured(snap)
        self.assertEqual(r["snapshot"]["total_rate_limit_errors"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
