# AGENTS.md — tokenmin-scanner

Guidance for AI coding agents (Claude Code, Cursor, Codex, Devin, Cline, Aider, Windsurf, Copilot, Jules) working in this repository. Markdown only — no required fields per [agents.md](https://agents.md/).

## Project overview

`watsonrm/tokenmin-scanner` is the public, Apache-2.0 source of **Tokenmin** — a Claude-usage advisor. It walks `~/.claude/` (or a chat export), anonymizes everything that isn't a count or a public model name, runs a deterministic rule base against the anonymized snapshot, and renders a ranked dollar-recoverable report to the terminal.

Tokenmin runs entirely on the user's machine by default. The hosted analyze endpoint on the roadmap (`ROADMAP.md`) is opt-in only. The static site at [tokenmin.ai](https://tokenmin.ai) is a separate repo (`watsonrm/tokenmin-site`).

## How to run

Install for end users:

```bash
curl --proto '=https' --tlsv1.2 -fsSL https://tokenmin.ai/install.sh | bash
```

Run from a clone (development):

```bash
./tokenmin                  # scan + render inline
./tokenmin watch            # live dashboard
./tokenmin --selfcheck      # dump anonymizer rules
./tokenmin --snapshot snap.json   # write anonymized payload to disk
```

Run the engine directly against a snapshot (useful when developing rules):

```bash
python3 -c "import json; from engine.tokenmin_engine import analyze_structured; print(analyze_structured(json.load(open('snap.json'))))"
```

## Layout — which side are you touching

The split is load-bearing. Agents working in one side rarely need to touch the other:

| Concern | Where |
|---|---|
| Walking `~/.claude` sessions, settings, agents, skills, MCP config | `skills/tokenmin/analyzer.py` |
| Parsing claude.ai / Claude Desktop chat exports | `skills/tokenmin/analyzer_chat_export.py` |
| Anonymization (paths, secrets, labels, identifiers) | `skills/tokenmin/anonymize.py` |
| Orchestrator CLI: collect → anonymize → analyze → render | `skills/tokenmin/tokenmin.py` |
| Detection rule base | `engine/patterns.py` |
| Report rendering | `engine/report.py` |
| Pricing lookup | `engine/pricing.py` + `engine/pricing.json` |
| Local HTTP server skeleton (for `--submit-url` testing) | `server/tokenmin_server.py` |
| Wrapper script + auto-update + version + doctor | `tokenmin` |

The **scanner side** (`skills/tokenmin/`) handles collection + anonymization + orchestration. The **engine side** (`engine/`) is pure deterministic analysis over an anonymized snapshot — no I/O, no network, no `~/.claude` knowledge.

## Testing instructions

Tests are stdlib-only Python; no `pip install` needed.

```bash
bash tests/run.sh                          # full suite (CI invokes this)
python3 -m unittest discover tests         # alternative discovery-based run
python3 tests/test_scrubber.py             # run one module directly
```

CI runs on every push across Python **3.10 / 3.11 / 3.12** (`.github/workflows/ci.yml`). The suite includes:

- 13 property + CLI tests (idempotent scrub, secret-pattern coverage, ReDoS input cap, salt sensitivity, HTTPS-only enforcement, double-flag on `--no-anonymize`, chmod 0600 on snapshot writes)
- Deterministic `--selfcheck` output diffed against `tests/fixtures/selfcheck.expected.json`
- **Synthetic-leak gate**: builds a fake `~/.claude/` with planted client names + paths, runs the scanner, fails CI if any plaintext survives the scrubber

If you change `skills/tokenmin/anonymize.py`, `skills/tokenmin/tokenmin.py`, or any anonymization invariant, **run `bash tests/run.sh` locally before claiming the change works**. The synthetic-leak gate is the canonical correctness check.

## Code style

- Python 3.10+ only (CI matrix is 3.10 / 3.11 / 3.12). No 3.9 fallbacks.
- Pure stdlib unless there is no alternative. No `pip install` in the install path.
- Stdlib type hints (`list[str]`, `dict[str, int]`) — no `from __future__ import annotations` workaround.
- Functions over classes. Modules are flat — no package nesting deeper than one level.
- Tests are stdlib `unittest`; one module per concern; named `test_*.py`.
- Inline comments explain *why*, not *what*. Existing code is the style guide.

## Security considerations (load-bearing)

**Read `SECURITY.md` before touching anonymization code.** The anonymization model is a contract with users, not an implementation detail. Specific invariants:

1. **Never bypass `scrub_text` on a write or submission path.** Every byte that leaves the process — written to disk via `--snapshot`, posted via `--submit-url`, logged — must pass through the scrubber.
2. **Never strip the per-install salt.** `~/.tokenmin/.salt` is HMAC-keyed, chmod 0600, refuses to overwrite via `O_EXCL`. The salt is what makes hashes non-reversible across installs.
3. **The `--no-anonymize` flag requires both `--i-know-what-im-doing` AND refusal to combine with `--submit-url`.** Don't relax this.
4. **Snapshot files are chmod 0600 on write and refuse to overwrite without `--force`.** Don't relax this.
5. **The synthetic-leak gate in CI is the canonical correctness check.** If you change the scrubber and the gate doesn't catch a new leak vector you introduced, *add* a fixture that exercises it.

Threat model + response targets + disclosure path live in `SECURITY.md`. Disclosure goes to `security@rmwcommerce.com` with subject `[tokenmin]`; 2-business-day ack SLA, 14-day patch SLA for confirmed high-severity.

## Other things to know

- `main` is protected — no force-push, no branch deletion, linear history required.
- The wrapper script (`tokenmin`) shells into `python3 -m skills.tokenmin.tokenmin`. Most agent work is inside `skills/tokenmin/` and `engine/`; the wrapper rarely needs changes.
- `bin/sources.json` is the curated list of Claude-optimization sources the detector-research watcher monitors. Adding a source requires a `trust_reason`.
- The static site (homepage, install.sh, guides) lives at `watsonrm/tokenmin-site`, not here. Don't try to edit `install.sh` from this repo.

## See also

- `README.md` — user-facing overview and install
- `ROADMAP.md` — what's next (hosted endpoint on Vercel)
- `SECURITY.md` — threat model + disclosure
- `SPEC.md` — architecture, data model, hand-off protocol
- `LICENSING.md` — Apache-2.0 file layout
- `llms.txt` — curated index for AI coding agents
- `llms-full.txt` — concatenated docs for context-window paste
