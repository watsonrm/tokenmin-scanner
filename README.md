# Tokenmin Scanner

**Every Claude optimization doc, distilled into one command.** Anthropic publishes
the playbook — caching, parallel tools, MCP hygiene, model routing — scattered
across their docs, engineering blog, and changelog. Tokenmin reads them so you
don't have to, watches your actual usage, and shows you the next dollar you can
save.

This repo is the **public, Apache-2.0 source** of Tokenmin — scanner, engine,
and local server skeleton in one place. The code that decides what (if anything)
leaves your machine is the code you can read. About 5 minutes end to end.

Tokenmin runs entirely on your machine by default. Nothing is sent anywhere
unless you opt in via `--submit-url` (and a hosted endpoint isn't live yet —
see [`ROADMAP.md`](ROADMAP.md)).

```bash
curl --proto '=https' --tlsv1.2 -fsSL https://tokenmin.ai/install.sh | bash
```

## What you get in the first 60 seconds

```
~ tokenmin
  ▶ scanning ~/.claude
  ✓ found 57 sessions in last 14 days
  ✓ anonymized
  ✓ analyzed

  Tokenmin  Claude usage audit
  ────────────────────────────────────────────────────────────────────────
  scanned 57 sessions over 14 days
  est. spend (window): $6,860
  model mix: Opus 99% · Sonnet 1%
  ────────────────────────────────────────────────────────────────────────

  Headline  ~$7,151/mo recoverable across 7 fix(es), ~4.8 hrs total

  1. A lot of your spend is on Opus — route by tier
     $$$$   ▮▮▮▮▮▮▮▮▮▮  $7,055/mo  0.1 hrs · conf 55% · model routing
     evidence: 100% of $6,860 weekly spend on Opus across 52 sessions.
     → tokenmin show model_overspend
  ...
```

A rich terminal card with the headline dollar figure, ranked findings,
severity pills, per-finding next-action. Then `tokenmin show <id>` drills
into one finding's evidence + fix. Then `tokenmin watch` runs a live
dashboard while you work.

## Your Tokenmin Score

Every run grades your setup with a single **Tokenmin Score** — a letter grade
(A+ … F), a 0–100 number, four pillar sub-scores, and a named tier:

```
  Tokenmin Score  C- 71/100  ·  Solid Operator
  Context & config 92  ·  Model routing 77  ·  Parallelism & MCP 58  ·  Density 50
```

The rubric is deterministic and public — see [`SCORING.md`](SCORING.md). A grade
is only worth sharing if you can see how it was computed.

```bash
tokenmin share            # render a shareable scorecard (SVG + HTML + PNG)
```

`tokenmin share` writes a 1200×630 social card to `~/.tokenmin/exports/`
(plus a browser HTML view with a copy-caption button). Like every Tokenmin
output it contains only aggregate numbers — no paths, names, or content.

## What's in here

| Concern | Where |
|---|---|
| Walking `~/.claude` sessions, settings, agents, skills, MCP config | [`skills/tokenmin/analyzer.py`](skills/tokenmin/analyzer.py) |
| Parsing claude.ai / Claude Desktop chat exports | [`skills/tokenmin/analyzer_chat_export.py`](skills/tokenmin/analyzer_chat_export.py) |
| Anonymization (paths, secrets, labels, identifiers) | [`skills/tokenmin/anonymize.py`](skills/tokenmin/anonymize.py) |
| Orchestrator CLI: collect → anonymize → analyze → render | [`skills/tokenmin/tokenmin.py`](skills/tokenmin/tokenmin.py) |
| Detection rule base | [`engine/patterns.py`](engine/patterns.py) |
| Tokenmin Score (composite grade rubric) | [`engine/scoring.py`](engine/scoring.py) · [`SCORING.md`](SCORING.md) |
| Shareable scorecard (SVG / HTML / PNG) | [`engine/scorecard.py`](engine/scorecard.py) |
| Report rendering | [`engine/report.py`](engine/report.py) |
| Pricing lookup | [`engine/pricing.py`](engine/pricing.py) + [`engine/pricing.json`](engine/pricing.json) |
| Local HTTP server skeleton (for `--submit-url` testing) | [`server/tokenmin_server.py`](server/tokenmin_server.py) |
| Wrapper script + auto-update + version + doctor | [`tokenmin`](tokenmin) |
| Tests + CI | [`tests/`](tests/), [`.github/workflows/ci.yml`](.github/workflows/ci.yml) |

The default run produces a finished report locally. `--snapshot snap.json`
writes the anonymized payload to disk if you want to audit what would be
sent before opting into a future hosted endpoint.

## Install

**Quick** (trusts the network all the way to GitHub):

```bash
curl --proto '=https' --tlsv1.2 -fsSL https://tokenmin.ai/install.sh | bash
```

**Verify-then-run** (recommended if you don't trust the network all the way to GitHub):

```bash
curl --proto '=https' --tlsv1.2 -fsSL -o install.sh https://tokenmin.ai/install.sh
curl --proto '=https' --tlsv1.2 -fsSL -o install.sh.sha256 https://tokenmin.ai/install.sh.sha256
shasum -a 256 -c install.sh.sha256
less install.sh
bash install.sh
```

The installer detects every Claude variant on your machine (Code / Desktop on
macOS / Linux / Windows), drops a single `tokenmin` command on PATH, and offers
to add it to your shell rc with consent. No `gh`, no `brew`, no auth setup.

After install:

```bash
tokenmin --selfcheck      # see the anonymizer rules without reading Python
tokenmin                  # scan + render inline (the magic moment)
tokenmin share            # render a shareable scorecard (SVG + HTML + PNG)
tokenmin watch            # live dashboard
tokenmin show <id>        # drill into one finding
tokenmin help             # 30-second walkthrough
```

## Trust posture

### Hashes are HMAC-keyed, not raw SHA-256

Identifiers (file paths, project names, MCP server names, custom agent /
skill / command names) hash with HMAC-SHA256 keyed by a **32-byte salt
generated on first run** at `~/.tokenmin/.salt` (chmod 0600, refuses to
overwrite via `O_EXCL`). Output is 16 hex chars (64 bits) — collision-resistant
for any realistic corpus.

An adversary who guesses common path names like `~/.ssh/known_hosts` *cannot*
precompute its hash without your salt. Cross-snapshot correlation works within
your install (so the engine can flag "same file re-read 12×"); cross-user
correlation is broken.

Stricter mode: `TOKENMIN_STRICT_ANONYMIZE=1` adds an additional per-run salt.
Breaks within-user cross-run correlation too at the cost of losing
across-days findings.

### Defense-in-depth on inputs

Pathological JSONL inputs (oversized lines, regex-bomb strings, malformed
JSON) can't hang the scrubber: every regex sees inputs truncated to 64 KiB
max; bad lines are dropped, not raised on; recursion depth is capped.

### Audit log

Every snapshot built + every submission writes a JSON line to
`~/.tokenmin/audit.log` (chmod 0600) with UTC timestamp, event, and SHA-256
digest of the payload. **Never user content.** After the fact you can
reconstruct exactly what bytes you sent and when.

### Transport defaults

- Default `tokenmin` mode is local — no network calls at all
- `--submit-url` refuses `http://` for non-localhost
- `--api-key-env VAR` keeps bearer tokens out of `ps` / shell history
- `--no-anonymize` requires `--i-know-what-im-doing` AND refuses to combine with `--submit-url`
- `--snapshot FILE` writes chmod 0600 + refuses to overwrite without `--force`

### Continuous verification

[![CI](https://github.com/watsonrm/tokenmin-scanner/actions/workflows/ci.yml/badge.svg)](https://github.com/watsonrm/tokenmin-scanner/actions/workflows/ci.yml)

Every push runs across Python 3.10 / 3.11 / 3.12:
- 13 property + CLI tests including idempotent scrub, secret-pattern coverage
  (Anthropic / OpenAI / Stripe / JWT / npm / Google / AWS / GitHub / Slack),
  ReDoS input cap, salt sensitivity + stability, HTTPS-only enforcement,
  double-flag on `--no-anonymize`, chmod 0600 on snapshot writes
- Deterministic `--selfcheck` output diffed against `tests/fixtures/selfcheck.expected.json`
- Synthetic-leak gate: builds a fake `~/.claude/` with planted client names + paths,
  runs the scanner, fails CI if any plaintext survives the scrubber

### Branch protection

`main` is protected: no force-push, no branch deletion, linear history required.

### Full security policy

[`SECURITY.md`](SECURITY.md) covers threat model, response targets (2-day ack,
5-day triage, 14-day patch for confirmed high-severity), supported versions,
named limitations.

## What gets collected, in full

| Field | Form |
|---|---|
| Session counts, turn counts | integer |
| Tool call counts by name | integer per tool name (MCP tool names hashed) |
| File paths from Read/Write/Edit | whole-string HMAC hash, no suffix leak |
| Project field, MCP servers, custom agents/skills/commands | whole-string HMAC hash |
| Models used, token usage, USD cost estimate | as-is (public info) |
| Permission denies, error results, redo signals | integer count |
| Timestamps | session start/end |

What never reaches the snapshot:

| Field | Why not |
|---|---|
| Raw text of user prompts | only lowercased + keyword-counted in memory for the redo-signal scan, then discarded |
| Raw assistant responses | scanner never reads them |
| Tool results | scanner never reads them |
| Anything outside `~/.claude/` | not in scan scope |
| Secrets (Anthropic / OpenAI / Stripe / JWT / npm / Google / AWS / GitHub / Slack tokens, PEM blocks, emails, IPs) | scrubbed by `anonymize.py` before any write |

Run `tokenmin --selfcheck` to see the exact anonymization output for a fixed
set of sample inputs. No collection happens.

## Roadmap

See [`ROADMAP.md`](ROADMAP.md) for what's next. Highlights:

- **Hosted analyze endpoint (Vercel)** — opt-in cloud endpoint with snapshot
  persistence, so users can submit anonymized snapshots for shared-rule-base
  analysis without anything more than a public-package install. Local engine
  stays the default and the offline fallback.
- **Native Claude Desktop adapter** — today Desktop users go through the
  chat-export path (same as web). Live Electron-store parsing is in progress.
- **Rule-base community contributions** once enough usage data validates
  which rules carry their weight.

## Detector research pipeline

The detector rule-base grows from a weekly scan of Anthropic's docs / engineering
blog + a curated allowlist of community sources. The pipeline runs in GitHub
Actions (`.github/workflows/detector-research.yml`) every Monday and is two stages:

1. **`bin/detector-research.py`** — URL discovery. Diffs the curated source list
   (`bin/sources.json`) against `bin/.research-seen.json`, writes the fresh URLs
   to `bin/.research-fresh.json`. Pure stdlib; no per-URL issues filed.
2. **`bin/detector-synthesize.py`** — Claude-judged synthesis. For each fresh
   URL: fetch the page, ask Claude (via the prompt in `bin/synthesis-prompt.md`)
   whether it suggests a new detector tokenmin doesn't already have. Files a
   structured `research-candidate` issue only on genuine signals; logs every
   verdict to a weekly digest issue.

Cost-capped at `$1.00/run` by default (`TOKENMIN_SYNTH_BUDGET`); model
selectable via `TOKENMIN_SYNTH_MODEL` (default `claude-sonnet-4-6`, or
`claude-haiku-4-5` for ~8× cheaper synthesis).

**Required one-time setup:** add the Anthropic API key as a repo secret.
Until this is set, stage 2 is a no-op:

```bash
gh secret set ANTHROPIC_API_KEY --repo watsonrm/tokenmin-scanner
```

Manual escape hatch: `python3 bin/detector-research.py --legacy-file-issues`
restores the original "one issue per fresh URL, no Claude judgment" behavior
if the synthesis stage is ever broken.

## Repos

| Repo | Visibility | Purpose |
|---|---|---|
| **`watsonrm/tokenmin-scanner`** (this) | public, Apache-2.0 | scanner + engine + server skeleton |
| `watsonrm/tokenmin-site` | public | static site served at https://tokenmin.ai |

## License

Apache-2.0 across the whole repo. See [`LICENSE`](LICENSE), [`NOTICE`](NOTICE),
and [`LICENSING.md`](LICENSING.md) for the layout.
