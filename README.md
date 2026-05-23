# Tokenmin Scanner

**The public, Apache-2.0 audit copy of Tokenmin.** This is the code that decides
what (if anything) leaves your machine when you run `tokenmin`. About 5 minutes
of reading, end to end.

The deal: Tokenmin is free during the friends-and-family preview in exchange for
your anonymized usage data. The scanner that does the anonymization is open
precisely so the trade is verifiable, not just promised. Read it. Diff it
against your install. Then decide if you trust the bargain.

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

## What's in here, what isn't

| Concern | Where |
|---|---|
| Walking `~/.claude` sessions, settings, agents, skills, MCP config | This repo — [`skills/tokenmin/analyzer.py`](skills/tokenmin/analyzer.py) |
| Parsing claude.ai / Claude Desktop chat exports | This repo — [`skills/tokenmin/analyzer_chat_export.py`](skills/tokenmin/analyzer_chat_export.py) |
| Anonymization (paths, secrets, labels, identifiers) | This repo — [`skills/tokenmin/anonymize.py`](skills/tokenmin/anonymize.py) |
| Orchestrator CLI: collect → anonymize → submit → render | This repo — [`skills/tokenmin/tokenmin.py`](skills/tokenmin/tokenmin.py) |
| Wrapper script + auto-update + version + doctor | This repo — [`tokenmin`](tokenmin) |
| Tests + CI | This repo — [`tests/`](tests/), [`.github/workflows/ci.yml`](.github/workflows/ci.yml) |
| **Detection rules**, scoring, report rendering | Not here. Lives in proprietary `watsonrm/tokenmin-core`. |
| Hosted submission server | Not here. Bundled in the F&F preview. |

This repo is **scanner-only**. Running it produces an anonymized snapshot. It
does *not* produce a report (that's the engine's job). The scanner is fully
functional without the engine — you can write the snapshot to disk with
`--snapshot snap.json` and audit what would be sent before deciding to submit
anywhere.

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

The F&F bundle mirrors these scanner files; its CI fails if they drift.

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

The F&F bargain ("free for anonymized data") only works if the install is
trivial. That's the lead priority.

- **Native Claude Desktop adapter** — today Desktop users go through the
  chat-export path (same as web). Live Electron-store parsing is in progress.
- **Hosted endpoint** — today the bundled `server/` stub runs locally for
  testing. A real `https://api.tokenmin.ai/analyze` endpoint with persistence
  + auth lands when F&F invitees cross ~5 yes-RSVPs.
- **Engine v0.5 detectors** — cache hit ratio, mid-session `/model` cache flush,
  output style presence, `ENABLE_TOOL_SEARCH` not set, subagent return-size
  overruns, 1M-context trap past 256K. (See the public guide for the
  underlying patterns.)
- **GPG-signed releases** for `TOKENMIN_REQUIRE_SIGNED=1` users.
- **One-line installer with embedded signature verification.**

## Repos

| Repo | Visibility | Purpose |
|---|---|---|
| **`watsonrm/tokenmin-scanner`** (this) | public, Apache-2.0 | scanner + anonymizer + CLI |
| `watsonrm/tokenmin` | private | F&F preview bundle (mirrors scanner files + ships engine + server) |
| `watsonrm/tokenmin-core` | private | proprietary engine + rule base + positioning |
| `watsonrm/tokenmin-site` | public | static site served at https://tokenmin.ai |

## License

Apache-2.0. See [`LICENSE`](LICENSE) and [`NOTICE`](NOTICE). The proprietary
engine in `watsonrm/tokenmin-core` is under a separate, non-OSS license. See
[`LICENSING.md`](LICENSING.md) for the boundary.
