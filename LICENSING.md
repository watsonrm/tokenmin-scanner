---
title: "Tokenmin — Component Layout"
status: active
type: reference
date: 2026-05-24
---

# Tokenmin — Component Layout

This repository (`watsonrm/tokenmin-scanner`) is **all of Tokenmin** — scanner,
engine, and the local server skeleton — licensed under **Apache-2.0** (see
[`LICENSE`](LICENSE) and [`NOTICE`](NOTICE)).

> **History note (2026-05-24).** The engine previously lived in a private
> repo and shipped to friends-and-family via a separate bundle. We relicensed
> it Apache-2.0 and merged it into this repo. The hosted endpoint that
> previously justified the closed-source engine is now a roadmap item rather
> than a built thing — see [`ROADMAP.md`](ROADMAP.md). The preview phase
> doesn't need server-side execution, and once it does, the value of a hosted
> service is the rule-base data flywheel, not the engine source.

## Pipeline

`collect → anonymize → analyze → render`. All four steps run on your machine
out of this single repo. In the future, the analyze + render steps can
optionally be delegated to a hosted endpoint (see ROADMAP); for now, every
install is fully self-contained.

```
   scanner                            engine                        terminal
   collect()  → Snapshot (raw)
        │
   anonymize() → Snapshot (scrubbed) ──in-process──►  detection + render
        │                                                     │
   display(report) ◄──────────────── report (Markdown) ◄──────┘
```

## Contract, in order of importance

1. **Anonymization happens before any hand-off** — before any network call in
   future hosted mode, before the in-process engine call in local mode.
   Because the scanner is open, this is verifiable, not just promised.
2. **Only the anonymized `Snapshot` is ever handed off.** No raw transcript
   content, no message text, no tool results.
3. **`Snapshot` is the shared input schema.** Its field definitions live in
   [`skills/tokenmin/analyzer.py`](skills/tokenmin/analyzer.py); detector
   logic, scoring, and rendering live in [`engine/`](engine/).

## File layout

| Path | Role |
|---|---|
| `skills/tokenmin/analyzer.py` | Claude Code collector — walks `~/.claude` |
| `skills/tokenmin/analyzer_chat_export.py` | claude.ai + Claude Desktop chat-export collector |
| `skills/tokenmin/analyzer_desktop_native.py` | Stub for the eventual native Desktop store parser |
| `skills/tokenmin/anonymize.py` | Scrubber — the trust guarantee itself |
| `skills/tokenmin/tokenmin.py` | Orchestrator + transport |
| `skills/tokenmin/SKILL.md` | `/tokenmin` Claude Code surface |
| `engine/tokenmin_engine.py` | Engine entry point (`analyze` / `analyze_structured`) |
| `engine/patterns.py` | Detection rule base |
| `engine/report.py` | Markdown report renderer |
| `engine/pricing.py` + `engine/pricing.json` | Model pricing lookup |
| `server/tokenmin_server.py` | Local HTTP wrapper (skeleton; production hosted endpoint is a roadmap item) |
| `tests/` | Property + CLI tests |
| `SPEC.md` | Scanner architecture + trust posture |
| `ROADMAP.md` | What's next (hosted endpoint at the top) |

## Trust posture

- The scanner is open by necessity, not as a marketing nicety. The code that
  decides what leaves your machine is the code you can read. Without that, the
  bargain is unverifiable.
- "Pseudonymized" is the honest word — hashes are stable across runs so the
  engine can correlate. For strict per-run anonymity at the cost of correlation,
  set `TOKENMIN_STRICT_ANONYMIZE=1`.
- Default runs touch the network zero times. A future hosted endpoint
  (`--submit-url`) is opt-in only.
- The scanner refuses to submit over HTTP for non-localhost endpoints, refuses
  to combine `--submit-url` with `--no-anonymize`, and supports `--api-key-env`
  so bearer tokens don't show up in `ps` / shell history.

## License

Apache-2.0 across the whole repo. See [`LICENSE`](LICENSE) and [`NOTICE`](NOTICE).
