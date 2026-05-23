---
title: "Tokenmin — Component Boundary"
status: active
type: reference
date: 2026-05-23
---

# Tokenmin — Component Boundary

This repository (`watsonrm/tokenmin-scanner`) is the **scanner** half of
Tokenmin: collector + anonymizer + transport. It is licensed under
**Apache-2.0** (see [`LICENSE`](LICENSE) and [`NOTICE`](NOTICE)).

The **engine** half — detection rule base, scoring, report rendering, hosted
service — is proprietary and lives separately in `watsonrm/tokenmin-core`. It
is **not** distributed in this repository.

## Boundary

`collect → anonymize → hand off → display`. The scanner does the first three.
Anything past the hand-off — the actual rules, ranking, and report text — is
the proprietary engine's job.

```
   scanner (this repo, Apache-2.0)               proprietary engine (separate)
   collect()  → Snapshot (raw)
        │
   anonymize() → Snapshot (scrubbed) ──hand-off──►  detection + render
        │                                                     │
   display(report) ◄──────────────── report (Markdown) ◄──────┘
```

In **local mode**, the hand-off is an in-process call into a separately-installed
engine module (`tokenmin_engine.analyze(snapshot) -> str`). In **hosted mode**,
the hand-off is an HTTPS POST to a Tokenmin service.

## Contract, in order of importance

1. **Anonymization happens in the scanner, before any hand-off** — before any
   network in hosted mode, before any in-process call in local mode. Because
   the scanner is open, this is verifiable, not just promised.
2. **Only the anonymized `Snapshot` is ever handed off.** No raw transcript
   content, no message text, no tool results.
3. **Detection never runs in open code.** The proprietary engine holds all
   rules; "detection is always proprietary" holds even with no network.
4. **`Snapshot` is the shared input schema.** Its field definitions live in
   the scanner ([`skills/tokenmin/analyzer.py`](skills/tokenmin/analyzer.py));
   all detector logic, scoring, and rendering live in the engine.

## File disposition

| File | Where | Why |
|---|---|---|
| `analyzer.py` | Scanner (here, Apache-2.0) | Claude Code collector |
| `analyzer_chat_export.py` | Scanner (here) | claude.ai + Claude Desktop chat-export collector |
| `analyzer_desktop_native.py` | Scanner (here) | Stub for the eventual native Desktop store parser |
| `anonymize.py` | Scanner (here) | The trust guarantee itself |
| `tokenmin.py` | Scanner (here) | Orchestrator + transport |
| `skills/tokenmin/SKILL.md` | Scanner (here) | `/tokenmin` Claude Code surface |
| `SPEC.md` | Scanner (here) | Scanner architecture + trust posture |
| Engine modules (rule base, scoring, report renderer, server) | Proprietary repo (NOT here) | The product IP |

## Trust posture

- The scanner is open by necessity, not as a marketing nicety. The code that
  decides what leaves your machine is the code you can read. Without that, the
  bargain is unverifiable.
- "Pseudonymized" is the honest word — hashes are stable across runs so the
  engine can correlate. For strict per-run anonymity at the cost of correlation,
  set `TOKENMIN_STRICT_ANONYMIZE=1`.
- Submission to a hosted endpoint is explicit and opt-in (`--submit-url`).
  Default runs touch the network zero times.
- The scanner refuses to submit over HTTP for non-localhost endpoints, refuses
  to combine `--submit-url` with `--no-anonymize`, and supports `--api-key-env`
  so bearer tokens don't show up in `ps` / shell history.

## License

- **This repo (`watsonrm/tokenmin-scanner`):** Apache-2.0.
- **Engine (`watsonrm/tokenmin-core`):** proprietary, all rights reserved.
