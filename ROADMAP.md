# Tokenmin Roadmap

Items in priority order. PRs and issues welcome.

## Now

(nothing in flight — preview phase, gathering F&F feedback)

## Shipped

### Tokenmin Score + shareable scorecard (v0.13)

A single composite grade (A+ … F · 0–100 · four pillar sub-scores · named
tier) over the optimizer pillars, with a published rubric ([`SCORING.md`](SCORING.md))
so the methodology is legible. `tokenmin share` renders a 1200×630 social
scorecard (SVG + browser HTML + PNG) to `~/.tokenmin/exports/`; aggregate
numbers only, safe to share by construction. Rubric lives in
[`engine/scoring.py`](engine/scoring.py); rendering in
[`engine/scorecard.py`](engine/scorecard.py).

## Next

### Hosted analyze endpoint (Vercel)

**Goal**: optional cloud endpoint that runs `engine.analyze_structured()` on a
submitted snapshot and returns the result, so users can opt into
telemetry-backed analysis without anything more than a public-package install.
Local engine remains the default and the offline fallback.

**Why Vercel**: deploys from GitHub on push. Python serverless functions
supported. Free hobby tier covers preview-scale by ~100×. Keeps the mental
model as "git push to deploy" — no new CLI to learn vs. Cloud Run or Fly.io.

**Trigger to start**: ~5 yes-RSVPs from active F&F recipients (the original
threshold), OR demonstrated demand for shared-rule-base analysis that can't be
done offline.

**Scope when triggered**:

- FastAPI app exposing `POST /analyze` (Bearer-token auth, snapshot in,
  structured findings + markdown out) and `GET /health`
- Snapshot persistence to a free-tier object store (Vercel Blob or an external
  bucket) for the rule-base data flywheel
- API key issuance via a small admin script + a key allowlist file (no GCP
  IAM, no PATs-in-public-repos)
- Custom domain `api.tokenmin.ai`
- Client gets `--remote` flag (or `TOKENMIN_REMOTE=1`) that submits the
  snapshot instead of running the engine locally; identical rendering

**Explicitly out of scope at preview phase**: server-side IP protection
(engine is Apache-2.0 in this repo), multi-region failover, real-time
analytics dashboard.

## Later

- Native Claude Desktop local-store adapter (Electron LevelDB parser; today
  users export-and-scan)
- Multi-Claude-install support beyond Claude Code (claude.ai already via
  `--source export`)
- Rule-base community contribution flow once enough usage data validates
  which rules carry their weight
- **Tokenmin Score percentile** — the scorecard reserves a "top N% of
  developers" line; it fills in once the anonymized corpus is large enough to
  compute honestly (gated on the hosted endpoint above). Never faked.

## Hosting decision log

- **2026-05-24** — Considered Fly.io (prior plan), Cloud Run (default GCP
  fit), then Vercel. Picked Vercel as the eventual hosted-endpoint platform
  because of GitHub-native deploys and zero new mental models. Documented in
  `ROADMAP.md` as the "Next" item rather than built immediately, because the
  engine is now Apache-2.0 in this repo and the preview phase doesn't need
  server-side execution.
