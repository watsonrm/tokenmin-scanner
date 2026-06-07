---
title: "Tokenmin Score — how the grade is computed"
status: active
type: reference
---

# The Tokenmin Score

Every `tokenmin` run produces one headline grade: a letter (**A+ … F**), a
**0–100** composite, four **pillar** sub-scores, and a named **tier**. This page
is the whole rubric. A grade is only worth sharing if you can see how it was
computed — so nothing here is hidden, and the math is deterministic (same
inputs → same grade, every time). The implementation is
[`engine/scoring.py`](engine/scoring.py); these constants are the source of truth.

## What it measures

The Tokenmin Score grades **workflow quality, not spend.** Two people on
different plans with identical habits get the same grade. Dollars stay in the
individual findings (where the cost framing belongs); they never gate the grade.
This is deliberate — an earlier dollar-weighted score made flat-fee (Pro/Max)
users look near-perfect and re-introduced the exact framing that erodes trust.

## The four pillars

Each pillar starts at **100** and loses points for the findings that land in it.

| Pillar | What it covers | Composite weight |
|---|---|---|
| 1 — Context & config | CLAUDE.md, context discipline, caching, `/clear` hygiene | **40%** |
| 2 — Model routing | Opus/Sonnet/Haiku routing, subagent model declarations | 20% |
| 3 — Parallelism & MCP | parallel tool calls, subagents, MCP hygiene | 20% |
| 4 — Density | output style, scoping, redundant reads/searches | 20% |

Pillar 1 carries the most weight because the optimizer's evidence puts ~80% of
the available gains there.

## How points come off

For each finding in a pillar:

```
deduction = base × confidence
```

- **base = 22** if the finding is *primary*, **7** if it's *minor*.
- A finding is **primary** when it is well-evidenced (`confidence ≥ 0.70`) — or,
  for a lower-confidence finding, when it's materially expensive
  (`≥ $25/mo` discounted, or `≥ 2%` of monthly API-equivalent cost). Confidence
  is the primary basis on purpose; dollars only *escalate* a weak finding.

Hygiene findings don't belong to a pillar; they apply a small flat penalty to
the composite (**−3 each, capped at −10**).

## The one measured signal: cache hit ratio

Pillar 1 also gets a bounded nudge from a directly *measured* quantity —
your cache hit ratio `cache_read / (cache_read + cache_write + input)`:

```
adjustment = (hit_ratio − 0.5) × 20      # clamped to ±10
```

So a 90% hit ratio adds ~+8 to Pillar 1; a 10% ratio subtracts ~−8. Caching is
the highest-leverage Claude Code cost lever Anthropic documents, so it moves the
grade whether or not a finding fired. (Needs ≥100K tokens of signal to apply.)

## Composite, grade, and tier

```
composite = 0.40·P1 + 0.20·P2 + 0.20·P3 + 0.20·P4 − hygiene_penalty
```

clamped to [0, 100], then mapped:

| Composite | Grade | | Composite | Tier |
|---|---|---|---|---|
| 97–100 | A+ | | 90–100 | Token Sommelier |
| 93–96 | A | | 80–89 | Dialed In |
| 90–92 | A− | | 70–79 | Solid Operator |
| 87–89 | B+ | | 60–69 | Leaving Money on the Table |
| 83–86 | B | | 45–59 | Context Hoarder |
| 80–82 | B− | | 0–44 | Setting Tokens on Fire |
| 77–79 | C+ | | | |
| 73–76 | C | | | |
| 70–72 | C− | | | |
| 60–69 | D (+/−) | | | |
| < 60 | F | | | |

## Provisional grades

On thin data — **fewer than 5 sessions** or **under 200K tokens** in the window
— the grade is marked *provisional*. The signal is too small to grade
confidently; re-run after a week of real use.

## Percentile (not yet live)

The scorecard reserves a "top N% of developers" line. It stays empty until a
large enough anonymized corpus exists to compute it honestly. We will not fake a
percentile.

## Rubric version

`rubric_version` is stamped into every score (currently **1.0**). When the
constants here change, that version bumps so older scorecards remain
interpretable.
