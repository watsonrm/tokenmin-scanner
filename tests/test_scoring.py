#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Tests for the Tokenmin Score (engine/scoring.py) + its wiring into
analyze_structured().

Guarantees:
  1. A clean snapshot (no findings, healthy cache) grades in the A range.
  2. Each material finding lowers the composite — monotonic, never raises it.
  3. The maturity gate marks thin data `provisional`.
  4. The measured cache-hit ratio nudges Pillar 1 (up when high, down when low).
  5. analyze_structured() returns a well-formed `tokenmin_score` block, and the
     grade letter is always one of the documented bands.

Stdlib-only; CI runs as `python3 tests/test_scoring.py`.
"""
from __future__ import annotations

import sys
import unittest
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SKILL_DIR = REPO_ROOT / "skills" / "tokenmin"
ENGINE_DIR = REPO_ROOT / "engine"
sys.path.insert(0, str(SKILL_DIR))
sys.path.insert(0, str(ENGINE_DIR))

import scoring  # noqa: E402
import tokenmin_engine  # noqa: E402

_VALID_GRADES = {"A+", "A", "A-", "B+", "B", "B-", "C+", "C", "C-", "D+", "D", "D-", "F"}


@dataclass
class FakeFinding:
    pillar: str
    savings_usd_per_month: float
    confidence: float


@dataclass
class FakeSession:
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    input_tokens: int = 0


@dataclass
class FakeSnap:
    sessions: list = field(default_factory=list)


def _mature_sessions(n=10, cache_read=2_000_000, cache_write=200_000, inp=300_000):
    # Spread the totals across n sessions so len(sessions) clears MIN_SESSIONS
    # and the token denom clears MIN_DENOM_TOKENS.
    return [FakeSession(cache_read // n, cache_write // n, inp // n) for _ in range(n)]


class TestScoring(unittest.TestCase):
    def test_clean_snapshot_is_A_range(self):
        snap = FakeSnap(_mature_sessions())
        score = scoring.compute_score(snap, [], monthly_api=100.0)
        self.assertFalse(score["provisional"])
        self.assertIn(score["grade"], {"A+", "A", "A-"})
        self.assertGreaterEqual(score["composite"], 90)
        self.assertEqual(score["tier"], "Token Sommelier")

    def test_findings_lower_the_grade_monotonically(self):
        snap = FakeSnap(_mature_sessions())
        prev = scoring.compute_score(snap, [], monthly_api=100.0)["composite"]
        findings = []
        for pillar in ("1", "2", "3", "4"):
            findings.append(FakeFinding(pillar, savings_usd_per_month=200.0, confidence=0.9))
            cur = scoring.compute_score(snap, findings, monthly_api=100.0)["composite"]
            self.assertLessEqual(cur, prev, f"adding a finding raised the score at pillar {pillar}")
            prev = cur
        self.assertLess(prev, 90)

    def test_provisional_gate_on_thin_data(self):
        snap = FakeSnap([FakeSession(1000, 100, 5000)])  # 1 session, tiny denom
        score = scoring.compute_score(snap, [], monthly_api=0.0)
        self.assertTrue(score["provisional"])

    def test_cache_ratio_nudges_pillar1(self):
        high = FakeSnap([FakeSession(cache_read_tokens=950_000, cache_write_tokens=10_000, input_tokens=40_000)
                         for _ in range(6)])
        low = FakeSnap([FakeSession(cache_read_tokens=50_000, cache_write_tokens=300_000, input_tokens=650_000)
                        for _ in range(6)])
        p1_high = scoring.compute_score(high, [], monthly_api=10.0)["pillars"]["1"]
        p1_low = scoring.compute_score(low, [], monthly_api=10.0)["pillars"]["1"]
        self.assertGreater(p1_high, p1_low)

    def test_grade_letter_always_valid(self):
        snap = FakeSnap(_mature_sessions())
        for n in range(0, 12):
            findings = [FakeFinding("1", 500.0, 0.95) for _ in range(n)]
            score = scoring.compute_score(snap, findings, monthly_api=100.0)
            self.assertIn(score["grade"], _VALID_GRADES)
            self.assertGreaterEqual(score["composite"], 0)
            self.assertLessEqual(score["composite"], 100)

    def test_analyze_structured_includes_score_block(self):
        # Minimal snapshot dict in the shape tokenmin.py serializes.
        snapshot = {
            "generated_at": 0.0,
            "window_days": 30,
            "parse_errors": 0,
            "skipped_files": 0,
            "config": {"has_global_claude_md": True, "global_claude_md_lines": 120},
            "sessions": [
                {
                    "session_id": f"s{i}",
                    "project": "p",
                    "user_turns": 10,
                    "assistant_turns": 10,
                    "input_tokens": 30_000,
                    "output_tokens": 5_000,
                    "cache_read_tokens": 200_000,
                    "cache_write_tokens": 20_000,
                    "models_used": {"claude-sonnet-4-6": 10},
                    "tools_per_turn": [2, 3, 2],
                }
                for i in range(8)
            ],
        }
        result = tokenmin_engine.analyze_structured(snapshot, billing_plan="api")
        self.assertIn("tokenmin_score", result)
        ts = result["tokenmin_score"]
        for key in ("composite", "grade", "tier", "pillars", "provisional", "rubric_version"):
            self.assertIn(key, ts)
        self.assertIn(ts["grade"], _VALID_GRADES)
        # The grade must also appear in the rendered markdown.
        self.assertIn("Your Tokenmin Score", result["report_md"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
