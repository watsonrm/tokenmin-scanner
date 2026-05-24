#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Tests for v0.12.3's plan-aware cost framing + externalized pricing.

What broke before v0.12.3 (and what these tests guard against):

  * Reports showed `$X spent` to flat-fee Pro/Max users — accurate at API
    rates, misleading on a subscription. Made Rick distrust the tool when
    he saw $7,622 for a billing period where he actually paid $200.
  * Pricing tables were baked into source: any Anthropic rate change
    silently broke every installed tokenmin until users updated.

v0.12.3 guarantees this file asserts:

  1. Pro/Max reports contain NO dollar amounts in TL;DR or savings cards
     (subscription users see quota stretch % + tokens + hours instead).
  2. API/unknown reports continue showing dollars (no regression).
  3. Pricing loads from engine/pricing.json, not hardcoded constants.
  4. is_stale() fires when last_updated is older than stale_warning_days.
  5. Unknown models fall back to the fallback family in pricing.json.

Stdlib-only; CI runs as `python3 tests/test_cost_framing.py`.
"""
from __future__ import annotations

import json
import re
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parent.parent
SKILL_DIR = REPO_ROOT / "skills" / "tokenmin"
ENGINE_DIR = REPO_ROOT / "engine"
sys.path.insert(0, str(SKILL_DIR))
sys.path.insert(0, str(ENGINE_DIR))

# This file tests engine-level behavior (analyze_structured, pricing.json).
# The scanner-only repo (watsonrm/tokenmin-scanner) doesn't ship the engine,
# so skip the whole file there. The scanner has its own pricing fallback path
# (analyzer.py's static _STATIC_PRICING) that doesn't need engine-tier tests.
if not ENGINE_DIR.is_dir():
    print("test_cost_framing: skipping — engine not bundled (scanner-only repo)")
    raise SystemExit(0)

import pricing  # noqa: E402
from tokenmin_engine import analyze_structured  # noqa: E402


# ---------- shared snapshot fixture ------------------------------------------

def _opus_heavy_snapshot(n_sessions: int = 61, days: int = 14) -> dict:
    """61 Opus sessions over 14 days. Matches the shape Rick was on when he
    saw '$7,622 spent' and rightly objected."""
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
                "long_searches": 0, "agents_used": {},
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


# ---------- pricing.json + lookup --------------------------------------------

class PricingExternalization(unittest.TestCase):
    """C: pricing must come from pricing.json, not constants."""

    def test_pricing_json_exists_and_parses(self):
        path = ENGINE_DIR / "pricing.json"
        self.assertTrue(path.is_file(), "engine/pricing.json must exist")
        data = json.loads(path.read_text())
        # Required fields per schema_version 1
        self.assertEqual(data.get("schema_version"), 1)
        self.assertIn("last_updated", data)
        self.assertIn("models", data)
        for fam in ("opus", "sonnet", "haiku"):
            m = data["models"][fam]
            for key in ("input", "output", "cache_write", "cache_read"):
                self.assertIsInstance(m[key], (int, float),
                                      f"{fam}.{key} must be numeric")

    def test_price_for_returns_4tuple(self):
        prices = pricing.price_for("claude-opus-4-5")
        self.assertEqual(len(prices), 4)
        for p in prices:
            self.assertIsInstance(p, float)
        # Cache-read must be ~10% of input (the established discount ratio).
        # If this assertion ever fails, Anthropic changed the cache pricing
        # model and the engine's monthly_factor math needs review.
        inp, _out, _cw, cr = prices
        self.assertAlmostEqual(cr / inp, 0.10, places=2,
                               msg="cache-read should be ~10% of input rate")

    def test_unknown_model_falls_back(self):
        # An unrecognized model family must not return zeros — that would
        # silently zero out a user's reported cost.
        future = pricing.price_for("claude-future-9-mega-mind")
        self.assertGreater(future[0], 0)
        self.assertGreater(future[1], 0)

    def test_pricing_age_is_int_or_none(self):
        age = pricing.pricing_age_days()
        self.assertTrue(age is None or isinstance(age, int))

    def test_is_stale_respects_threshold(self):
        # Hot-swap the cached pricing with synthetic last_updated values.
        original_cache = pricing._cache
        try:
            future_recent = datetime.now(timezone.utc).date().isoformat()
            pricing._cache = {**pricing._load(), "last_updated": future_recent,
                              "stale_warning_days": 60}
            self.assertFalse(pricing.is_stale())

            old = (datetime.now(timezone.utc) - timedelta(days=120)).date().isoformat()
            pricing._cache = {**pricing._load(), "last_updated": old,
                              "stale_warning_days": 60}
            self.assertTrue(pricing.is_stale())
        finally:
            pricing._cache = original_cache


# ---------- plan-aware report framing (E) ------------------------------------

_DOLLAR_RE = re.compile(r"\$[\d,]+")


class PlanAwareReportFraming(unittest.TestCase):
    """E: Pro/Max reports must NOT contain dollar amounts in savings copy.
    API/unknown reports must continue to show dollars."""

    @classmethod
    def setUpClass(cls):
        snap = _opus_heavy_snapshot()
        cls.results = {
            plan: analyze_structured(snap, billing_plan=plan)
            for plan in ("api", "pro", "max", "unknown")
        }

    def _tldr_block(self, md: str) -> str:
        # Pull just the TL;DR section (between '## TL;DR' and the next '## ').
        m = re.search(r"## TL;DR\n(.*?)\n## ", md, flags=re.DOTALL)
        self.assertIsNotNone(m, "report missing TL;DR section")
        return m.group(1)

    def _snapshot_block(self, md: str) -> str:
        m = re.search(r"## Usage snapshot\n(.*?)\n## ", md, flags=re.DOTALL)
        self.assertIsNotNone(m, "report missing snapshot section")
        return m.group(1)

    def _recommendations_block(self, md: str) -> str:
        m = re.search(r"## Recommendations \(ranked\)\n(.*?)\n---", md, flags=re.DOTALL)
        self.assertIsNotNone(m, "report missing recommendations section")
        return m.group(1)

    def test_pro_tldr_has_no_dollar_signs(self):
        tldr = self._tldr_block(self.results["pro"]["report_md"])
        self.assertNotRegex(tldr, _DOLLAR_RE,
                            f"Pro TL;DR leaked a dollar amount:\n{tldr}")
        self.assertIn("quota", tldr, "Pro TL;DR must frame savings as quota")

    def test_max_tldr_has_no_dollar_signs(self):
        tldr = self._tldr_block(self.results["max"]["report_md"])
        self.assertNotRegex(tldr, _DOLLAR_RE,
                            f"Max TL;DR leaked a dollar amount:\n{tldr}")
        self.assertIn("quota", tldr)

    def test_api_tldr_keeps_dollars(self):
        # Regression guard: API users still need dollar framing.
        tldr = self._tldr_block(self.results["api"]["report_md"])
        self.assertRegex(tldr, _DOLLAR_RE,
                         "API TL;DR must keep dollar framing")

    def test_unknown_tldr_keeps_dollars(self):
        # Until the user picks a plan, default to dollars (safest assumption:
        # they're on metered API). The plan-note row nudges them to set it.
        tldr = self._tldr_block(self.results["unknown"]["report_md"])
        self.assertRegex(tldr, _DOLLAR_RE)

    def test_pro_snapshot_hides_dollar_row(self):
        snap = self._snapshot_block(self.results["pro"]["report_md"])
        self.assertNotIn("API-equivalent cost", snap,
                         "Pro snapshot should not show the dollar cost row")
        self.assertIn("Claude Pro", snap, "plan label missing")

    def test_max_snapshot_hides_dollar_row(self):
        snap = self._snapshot_block(self.results["max"]["report_md"])
        self.assertNotIn("API-equivalent cost", snap,
                         "Max snapshot should not show the dollar cost row")

    def test_api_snapshot_shows_dollar_row(self):
        snap = self._snapshot_block(self.results["api"]["report_md"])
        self.assertIn("API-equivalent cost", snap)
        # And the label is now honest ("API-equivalent") — not the bare
        # "Est. cost" that misled Rick.
        self.assertNotIn("| Est. cost ", snap)

    def test_pro_recommendations_show_quota_not_dollars(self):
        recs = self._recommendations_block(self.results["pro"]["report_md"])
        self.assertNotRegex(recs, _DOLLAR_RE,
                            f"Pro recommendations leaked dollars:\n{recs[:400]}")
        self.assertIn("quota", recs)

    def test_api_recommendations_keep_dollars(self):
        recs = self._recommendations_block(self.results["api"]["report_md"])
        self.assertRegex(recs, _DOLLAR_RE)


# ---------- engine schema includes monthly_api_equivalent + billing_plan -----

class EngineSchemaSurface(unittest.TestCase):
    """The CLI's terminal renderer relies on these fields — guard them."""

    def test_result_carries_billing_plan(self):
        for plan in ("api", "pro", "max", "unknown"):
            r = analyze_structured(_opus_heavy_snapshot(), billing_plan=plan)
            self.assertEqual(r["billing_plan"], plan)

    def test_snapshot_carries_monthly_api_equivalent(self):
        r = analyze_structured(_opus_heavy_snapshot())
        snap = r["snapshot"]
        self.assertIn("monthly_api_equivalent_cost_usd", snap)
        # 14-day window * 30/14 ≈ 2.14x the window cost
        ratio = snap["monthly_api_equivalent_cost_usd"] / max(snap["total_cost_usd"], 1)
        self.assertAlmostEqual(ratio, 30 / 14, places=1)

    def test_engine_version_advanced(self):
        r = analyze_structured(_opus_heavy_snapshot())
        # Bumped from 0.4 -> 0.5 with the cost-framing redesign.
        self.assertEqual(r["engine_version"], "0.5")

    def test_invalid_plan_normalizes_to_unknown(self):
        r = analyze_structured(_opus_heavy_snapshot(), billing_plan="bogus")
        self.assertEqual(r["billing_plan"], "unknown")


if __name__ == "__main__":
    unittest.main(verbosity=2)
