---
title: "Tokenmin — design spec, data model, trust posture, hand-off protocol"
status: spec
type: spec
version: 0.3
date: 2026-05-24
note: "Tokenmin is Apache-2.0 across the board (scanner + engine + server skeleton). See LICENSING.md for the file layout."
tags: [spec, tokenmin, claude-code, local-first]
---

# Tokenmin — design spec

## What this is

Tokenmin is Apache-2.0 across the board (see [LICENSING.md](LICENSING.md)). This
document specifies the architecture: scanner (collector + anonymizer + CLI) and
engine (detection rule base + report renderer) running locally as a single tool.
A future opt-in hosted endpoint is on the roadmap (`ROADMAP.md`); for now every
install is fully self-contained.

## Architecture

Pipeline, every stage a pure function over the previous stage's output, with one
boundary stage (anonymize) that must run before any hand-off.

```
~/.claude/projects/*/*.jsonl   ──►  [collect]   analyzer.py → Snapshot (raw)
~/.claude/settings.json        ──►                     │
~/.claude/CLAUDE.md            ──►                     ▼
~/.claude/agents/, skills/...  ──►            [anonymize]  anonymize.py
                                              scrub_value(...)  (mandatory boundary)
                                                       │
                                                       ▼
                                              Snapshot (scrubbed)
                                                       │
                              ┌────────────────────────┼────────────────────────┐
                              ▼                         ▼                         ▼
                       --snapshot PATH           local engine             --submit-url
                       (write JSON,            (engine/ in this repo,    (HTTPS POST to
                        no engine)              the default path)         a hosted endpoint)
                              │                         │                         │
                              ▼                         ▼                         ▼
                       snap.json               report (Markdown)         report (Markdown)
                                                       │                         │
                                                       └────────► display ◄──────┘
                                                                  tokenmin_report.md
```

| File | Role | Public surface |
|---|---|---|
| `tokenmin.py` | CLI orchestrator: collect → anonymize → hand off → display | `main(argv) -> int` |
| `analyzer.py` | Reads disk, builds `Snapshot` | `collect(home: Path, days: int) -> Snapshot` |
| `anonymize.py` | Boundary that scrubs paths/names/secrets/identifiers | `scrub_text`, `scrub_path`, `scrub_dict`, `scrub_value` (idempotent) |

**Boundary contract:** everything that leaves `analyzer.py`'s output — written to
`--snapshot`, passed to a local engine, or POSTed to `--submit-url` — goes
through `anonymize.py` first. The only exception is `--no-anonymize`, which is
local-debug only and refuses to submit (exit code 3).

## Data model

### `Snapshot` ([analyzer.py](skills/tokenmin/analyzer.py))

```python
@dataclass
class Snapshot:
    generated_at: float
    window_days: int
    sessions: list[SessionStats]
    config: ConfigSnapshot
    parse_errors: int = 0
    skipped_files: int = 0
    # derived properties: total_cost, total_input_tokens, total_output_tokens, tool_mix
```

### `SessionStats`

Per-session aggregate, built from one `~/.claude/projects/<proj>/<sid>.jsonl`.
Defensive to schema drift — anything the parser can't read is counted in
`parse_errors` and skipped.

| Field | Type | Meaning |
|---|---|---|
| `session_id` | str | filename stem of the jsonl |
| `project` | str | parent directory name (project slug) |
| `started_at`, `ended_at` | float \| None | UNIX timestamps of first/last messages |
| `user_turns`, `assistant_turns` | int | message counts |
| `tool_calls` | Counter | tool name → invocations |
| `tools_per_turn` | list[int] | one entry per assistant turn |
| `files_read` | Counter | path → Read count |
| `files_written` | set[str] | unique write targets |
| `permission_denies` | int | tool calls the user denied |
| `error_results` | int | tool calls that returned errors |
| `long_searches` | int | runs of 4+ consecutive search-tool calls |
| `agents_used` | Counter | subagent name → spawn count |
| `models_used` | Counter | model id → assistant turns on that model |
| `input_tokens`, `output_tokens`, `cache_write_tokens`, `cache_read_tokens` | int | sums |
| `est_cost_usd` | float | per-token pricing from `analyzer.PRICING`, summed |
| `redo_signals` | int | user-turn count of course-correction phrases |

### `ConfigSnapshot`

Per-user `~/.claude/` snapshot: presence of global settings/CLAUDE.md, line
counts, obsolete-feature references, hook/permission counts, custom
agents/skills/commands, MCP servers, and per-project rollups. See
[analyzer.py](skills/tokenmin/analyzer.py) for the full field list.

The `Snapshot` is the entire input contract between the open client and the
engine. The engine — local or hosted — agrees on these field names; it owns
everything done *with* them.

## Hand-off protocol

The client produces an anonymized `Snapshot` (serialized to JSON via
`tokenmin.py:_dataclass_to_dict` + `anonymize.scrub_value`) and hands it off:

- **`--snapshot PATH`** — write the JSON and stop. This is the exact payload an
  engine would see; useful for auditing what leaves the machine.
- **Local engine** — the default path. The client imports `tokenmin_engine` from
  `engine/` in this repo and calls `tokenmin_engine.analyze(snapshot: dict) -> str`,
  displaying the returned Markdown. No network.
- **`--submit-url URL`** (+ optional `--api-key`) — POST `{"snapshot": ...}` as
  JSON with an optional `Authorization: Bearer` header. The response is the
  report (Markdown directly, or `{"report": "..."}`). Refused if `--no-anonymize`.

If none is available, the client writes the snapshot (when asked) and exits 0
with guidance. It never fabricates findings.

## Trust posture

1. **Anonymization runs before any hand-off.** `tokenmin.py` calls
   `anonymize.scrub_value` on the snapshot before `--snapshot`, the local engine
   call, or `--submit-url`.
2. **`--no-anonymize` is local-debug only** and refuses to submit (exit 3).
3. **Patterns scrubbed** by `anonymize.py` (see the live `PATTERNS` list):
   Anthropic/generic API keys, GitHub/Slack/AWS/Google keys, bearer tokens,
   emails, IPs, home paths (`/Users/<USER>`, `/home/<USER>`, `C:\Users\<USER>`),
   and multi-segment paths → `<path:HASH8>/last-segment`.
4. **No raw transcript content.** The `Snapshot` carries counts and aggregates
   only; no user/assistant turn text is ever stored or sent.
5. **No network unless asked.** The client only reaches the network when
   `--submit-url` is passed.

## Surfaces

### CLI

```bash
python3 skills/tokenmin/tokenmin.py [--claude-home PATH] [--days N] \
    [--snapshot FILE] [--out FILE] [--submit-url URL] [--api-key K] [--no-anonymize]
```

Exit codes: `0` success, `2` `~/.claude` missing, `3` `--no-anonymize` + `--submit-url` rejected.

### `/tokenmin` slash command

Installed via the Claude Code plugin marketplace
(`claude plugin marketplace add watsonrm/tokenmin` + `claude plugin install tokenmin@tokenmin`).
Flow: locate `tokenmin.py` → run it (collect → anonymize → hand off) → if a report
was produced, summarize it and offer to implement the top recommendation; if no
engine is available, report that the anonymized snapshot is ready and that
findings need the engine. See [SKILL.md](skills/tokenmin/SKILL.md), which encodes the
trust rules (anonymize before send; never quote raw content; never invent
findings; refuse if `~/.claude/projects/` is empty).

## Engine surface (in `engine/`)

The engine lives at `engine/` in this repo (Apache-2.0). See
[LICENSING.md](LICENSING.md) for the file layout.

- Detection rule base — `engine/patterns.py`. Every detector and its logic.
- Scoring / ranking — `engine/patterns.py:Finding.score()`.
- Report rendering — `engine/report.py`. Turns findings into the Markdown report.
- Engine entry points — `engine/tokenmin_engine.py:analyze` (Markdown) and
  `analyze_structured` (structured findings + Markdown).
- Pricing lookup — `engine/pricing.py` + `engine/pricing.json`.
- Local HTTP server skeleton — `server/tokenmin_server.py`. Useful for testing
  the `--submit-url` path against a local endpoint. A production hosted
  endpoint is on the roadmap (`ROADMAP.md`).

The scanner is designed so the engine and server can both be replaced or
delegated to a remote service without the scanner caring.

## Cross-references

- [LICENSING.md](LICENSING.md) — file layout and trust posture
- [ROADMAP.md](ROADMAP.md) — what's next
- [Anthropic — Agent Skills](https://www.anthropic.com/engineering/equipping-agents-for-the-real-world-with-agent-skills) — the SKILL.md format the `/tokenmin` surface uses
- [Anthropic — CLAUDE.md memory docs](https://code.claude.com/docs/en/memory.md)
