"""Tokenmin Score — a composite 0–100 grade over the 4 optimizer pillars.

SPDX-License-Identifier: Apache-2.0

The Tokenmin Score turns the engine's findings into one memorable headline: a
letter grade (A+ … F), a 0–100 number, four pillar sub-scores, and a named
result tier. It is computed deterministically from the findings the detectors
already produce plus one measured signal (cache hit ratio).

The rubric is intentionally public — see SCORING.md. A grade is only worth
sharing if the way it was computed is legible; this module is the single source
of truth for the grade shown in the terminal, the markdown report, and the
shareable scorecard, so all three always agree.
"""
from __future__ import annotations

RUBRIC_VERSION = "1.0"

# --- tunable constants (the published rubric) -------------------------------

# Per-pillar deduction (out of 100), before confidence weighting.
PRIMARY_PTS = 22.0      # a material finding
LOW_PTS = 7.0           # a minor finding
# A finding counts as "primary" when it is well-evidenced (high confidence) OR
# materially expensive. Confidence is the PRIMARY basis on purpose: the grade
# measures workflow quality, not spend — keying severity on dollars alone made
# subscription / low-cost users grade as near-perfect and re-introduced the
# dollar framing that eroded trust. Dollars only *escalate* a low-confidence
# finding, they never gate the grade.
CONF_PRIMARY = 0.70     # confidence at/above this = primary
PRIMARY_USD = 25.0      # …or confidence-discounted $/mo at/above this
PRIMARY_QUOTA = 0.02    # …or >= 2% of monthly API-equivalent cost

# Composite weights. Pillar 1 (context + config) carries the most — consistent
# with the optimizer's "~80% of the gains live in Pillar 1" framing and the
# pillar_boost the finding ranker already applies.
WEIGHTS = {"1": 0.40, "2": 0.20, "3": 0.20, "4": 0.20}

# Hygiene findings don't own a pillar; they apply a small flat composite penalty.
HYGIENE_PTS = 3.0
HYGIENE_CAP = 10.0

# Cache hit ratio nudges Pillar 1 up/down (bounded). This is the one *measured*
# signal that moves the grade regardless of which findings fired — Anthropic's
# own engineering calls caching "everything" for Claude Code cost.
CACHE_MIN_DENOM = 100_000
CACHE_BONUS_CAP = 10.0

# Maturity gate — below this the grade is provisional (don't hand out a scary,
# confident F on a handful of sessions).
MIN_SESSIONS = 5
MIN_DENOM_TOKENS = 200_000

PILLAR_LABELS = {
    "1": "Context & config",
    "2": "Model routing",
    "3": "Parallelism & MCP",
    "4": "Density",
}

# Letter grade bands keyed on the composite 0–100.
_GRADE_BANDS = [
    (97, "A+"), (93, "A"), (90, "A-"),
    (87, "B+"), (83, "B"), (80, "B-"),
    (77, "C+"), (73, "C"), (70, "C-"),
    (67, "D+"), (63, "D"), (60, "D-"),
    (0,  "F"),
]

# Named result tiers by composite range — the shareable identity hook. People
# share an identity, not a metric. (Wording is Rick's to tune.)
_TIERS = [
    (90, "Token Sommelier"),
    (80, "Dialed In"),
    (70, "Solid Operator"),
    (60, "Leaving Money on the Table"),
    (45, "Context Hoarder"),
    (0,  "Setting Tokens on Fire"),
]


def _clamp(x: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, x))


def letter_grade(composite: float) -> str:
    for lo, g in _GRADE_BANDS:
        if composite >= lo:
            return g
    return "F"


def tier_for(composite: float) -> str:
    for lo, t in _TIERS:
        if composite >= lo:
            return t
    return _TIERS[-1][1]


def _is_primary(conf: float, disc: float, monthly_api: float) -> bool:
    if conf >= CONF_PRIMARY:
        return True
    if disc >= PRIMARY_USD:
        return True
    return monthly_api > 0 and (disc / monthly_api) >= PRIMARY_QUOTA


def compute_score(snap, findings, monthly_api: float = 0.0) -> dict:
    """Return the Tokenmin Score dict for a snapshot + its findings.

    `findings` is an iterable of objects exposing `.pillar`, `.confidence`, and
    `.savings_usd_per_month` (the engine's `Finding`). `snap` exposes
    `.sessions` (each with cache_read_tokens / cache_write_tokens /
    input_tokens). Pure + deterministic — same inputs, same grade.
    """
    pillars = {p: 100.0 for p in ("1", "2", "3", "4")}
    hygiene_penalty = 0.0

    for f in findings:
        pillar = str(getattr(f, "pillar", "hygiene"))
        save = float(getattr(f, "savings_usd_per_month", 0.0) or 0.0)
        conf = max(0.0, min(1.0, float(getattr(f, "confidence", 0.0) or 0.0)))
        disc = save * conf
        base = PRIMARY_PTS if _is_primary(conf, disc, monthly_api) else LOW_PTS
        penalty = base * conf
        if pillar in pillars:
            pillars[pillar] -= penalty
        else:
            hygiene_penalty += HYGIENE_PTS
    hygiene_penalty = min(hygiene_penalty, HYGIENE_CAP)

    # Measured cache-hit nudge to Pillar 1.
    cache_read = sum(getattr(s, "cache_read_tokens", 0) for s in snap.sessions)
    cache_write = sum(getattr(s, "cache_write_tokens", 0) for s in snap.sessions)
    input_tokens = sum(getattr(s, "input_tokens", 0) for s in snap.sessions)
    denom = cache_read + cache_write + input_tokens
    cache_hit_ratio = None
    if denom >= CACHE_MIN_DENOM:
        cache_hit_ratio = cache_read / denom
        # 0.5 = neutral; linearly scaled to ±CACHE_BONUS_CAP across [0,1].
        adj = (cache_hit_ratio - 0.5) * (2 * CACHE_BONUS_CAP)
        pillars["1"] += max(-CACHE_BONUS_CAP, min(CACHE_BONUS_CAP, adj))

    pillars_int = {p: int(round(_clamp(v))) for p, v in pillars.items()}

    composite = sum(pillars_int[p] * WEIGHTS[p] for p in pillars_int) - hygiene_penalty
    composite = _clamp(composite)
    composite_int = int(round(composite))

    provisional = len(snap.sessions) < MIN_SESSIONS or denom < MIN_DENOM_TOKENS

    return {
        "composite": composite_int,
        "grade": letter_grade(composite),
        "tier": tier_for(composite),
        "pillars": pillars_int,
        "pillar_labels": dict(PILLAR_LABELS),
        "weights": dict(WEIGHTS),
        "provisional": provisional,
        "percentile": None,            # filled when the telemetry corpus exists
        "cache_hit_ratio": cache_hit_ratio,
        "rubric_version": RUBRIC_VERSION,
    }
