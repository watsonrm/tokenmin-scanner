---
title: "Tokenmin — Security Policy"
status: active
type: reference
date: 2026-05-23
---

# Tokenmin — Security Policy

## Reporting a vulnerability

Email **security@rmwcommerce.com** with the subject line `[tokenmin]`. Include:

- a description of the issue and where you found it (file path + line number where possible)
- a proof-of-concept or reproduction steps
- your assessment of impact
- whether you'd like public credit

Encrypt sensitive reports with PGP if you prefer — request the current key by writing to that address first.

**Response targets:**
- Acknowledgement within **2 business days** of receipt
- Initial triage within **5 business days**
- For confirmed high-severity issues: patch released within **14 days**, with the public scanner repo (`watsonrm/tokenmin-scanner`) tagged with a `SECURITY-FIX` release annotation

We will not pursue legal action against good-faith researchers who:
- avoid privacy violations and service disruption
- give us a reasonable window to fix before public disclosure
- don't access or exfiltrate user data beyond what's needed to demonstrate the issue

## Supported versions

During the preview phase, **the `main` branch of `watsonrm/tokenmin-scanner` is the only supported version.** All security fixes land there first.

Auto-update default is interactive-prompt; users on `TOKENMIN_AUTOUPDATE=auto` get fixes within hours, users on prompt within a day, users on `off` only when they manually pull. If you operate Tokenmin at scale or in a security-sensitive environment, set `TOKENMIN_AUTOUPDATE=auto` *and* `TOKENMIN_REQUIRE_SIGNED=1`.

## Threat model

Tokenmin's threat model is **adversarial inputs + hostile network + curious but careful user**. We assume:

- The user's machine is not pre-compromised — if it is, we cannot defend against it.
- The user's Anthropic install (Claude Code, Desktop, claude.ai) is honest.
- The user's network is *potentially* hostile (cafe wifi, corporate MITM proxy, compromised DNS).
- The hosted endpoint, when one exists, is *moderately* trusted — we send it anonymized data but we don't trust it to be incorruptible.
- The git remote (`origin`) is trusted to the extent that GitHub is trusted, plus our signing-key discipline.

We **do not** defend against:
- An attacker with code-execution on the user's machine.
- A compromised Anthropic install feeding adversarial JSONL designed to confuse the analyzer.
- The hosted engine intentionally exfiltrating submitted data — that's the bargain; if you don't trust the engine, don't submit.

## What we do defend against

### Input handling

- **Adversarial session files.** A malicious `~/.claude/projects/*/*.jsonl` is parsed defensively: lines that don't parse are dropped; oversized strings are truncated before the scrubber sees them (`_MAX_SCRUB_LEN = 64 KiB`); the analyzer never raises on a bad line.
- **Adversarial chat-export zips.** We don't extract; we read `conversations.json` from the zip directly. Files larger than reasonable cause a SystemExit with a clear message rather than OOM.
- **Adversarial filesystem paths.** `--from` accepts only `.zip`, `.json`, or a directory containing `conversations.json`.

### Anonymization

- **Identifiers are HMAC-SHA256, not plain SHA-256.** The HMAC key is a 32-byte salt generated on first run (`~/.tokenmin/.salt`, chmod 0600) and stable across runs for cross-snapshot correlation. **Rainbow-table attacks fail** — an adversary who guesses `"~/.ssh/known_hosts"` cannot precompute its hash without the salt.
- **16 hex chars (64 bits) of hash** — collision-resistant for any realistic corpus.
- **Strict mode** (`TOKENMIN_STRICT_ANONYMIZE=1`) adds a per-run salt on top of the per-install salt, breaking even within-user cross-run correlation at the cost of losing "same file read 12× across days" findings.
- **Whole-string hashing for paths and identifiers** — filename suffixes do not leak.
- **Two-pass scrub** — label-hash known fields first, then free-text scrub remaining strings for paths, secrets, emails, IPs.

### Transport

- **HTTPS-only** for `--submit-url` (HTTP refused except for localhost).
- **API keys read from env** (`--api-key-env VAR`) rather than the command line, where they'd be visible in `ps` and shell history. The legacy `--api-key` is still accepted but warns.
- **No URL submission with `--no-anonymize`** — refused at the CLI.

### Defaults

- **`--no-anonymize` requires a second confirmation flag** (`--i-know-what-im-doing`).
- **Snapshot files are written `chmod 0600`** and **refuse to overwrite existing files** without `--force`.
- **No network access in the default `--out` mode** — the local engine runs in-process; nothing leaves the machine.

### Update channel

- **`git pull` over HTTPS** verifies TLS against the system trust store. To verify commit *authorship* (the missing piece in TLS alone), set `TOKENMIN_REQUIRE_SIGNED=1` — unsigned commits are then refused.
- **Auto-update default is interactive prompt** — surprising silent pulls don't happen.
- **5-second fetch timeout** — bad network never blocks a run.
- **Dirty working tree skipped** — never clobbers local changes.

### Audit log

Every run appends a JSON line to `~/.tokenmin/audit.log` (mode 0600):

```
{"ts": "2026-05-23T20:00:00+00:00", "event": "snapshot_built", "source": "code",
 "days": 30, "anonymized": true, "sessions": 53, "sha256": "abc...123"}
{"ts": "2026-05-23T20:00:01+00:00", "event": "submit_start",
 "url": "https://api.tokenmin.example/analyze", "sha256": "abc...123"}
{"ts": "2026-05-23T20:00:02+00:00", "event": "submit_ok",
 "url": "https://api.tokenmin.example/analyze", "sha256": "abc...123"}
```

The log records *what was sent* (by SHA-256 of the payload), *where*, and *when* — never user content. You can always reconstruct your submission history.

## Telemetry (v0.10+)

Tokenmin sends a fixed-fields anonymous usage signal so we can rank detectors
by real-world fire rate and surface install / crash bugs. The full per-field
dictionary is enumerated below — read it once, decide if you trust the trade.

### Defaults

**Off by default; asked on first interactive run.** Same iPhone-Diagnostics-style
consent: full disclosure (the field list below) + explicit y/N + easy reversal.

### Always-respected overrides

- `TOKENMIN_NO_TELEMETRY=1` env var — wins over anything in settings.json
- `tokenmin telemetry off` — permanent disable, persisted to settings
- No endpoint configured — events are formed but not transmitted (the default ship state today; an endpoint will be added when one is deployed)

### What's sent — fixed list

One event per `tokenmin` run, at the end. Schema `tokenmin.telemetry.v1`:

| Field | Example | Why |
|---|---|---|
| `schema` | `"tokenmin.telemetry.v1"` | schema versioning |
| `sent_at` | `"2026-05-23T22:30:00Z"` | request timing |
| `install_id` | `"98aae1d4566e5b27"` | HMAC-derived from your salt + a separate "install-id-v1" tag. **Not the salt itself; not your anonymization hash output** — different value space so it can never reveal a path or filename. Lets us dedupe a single install across daily events without re-identifying you. |
| `version` | `"0.10.0"` | bug routing |
| `platform` | `"Darwin 25.4.0"` | compatibility |
| `python_version` | `"3.12"` | compatibility |
| `subcommand` | `"run"`, `"watch"`, `"show"`, `"demo"` | feature usage ranking |
| `findings_fired` | `["model_overspend","no_output_style"]` | detector ranking — **id list only, never the values** |
| `session_count_bucket` | `"1-10"`, `"11-100"`, `"101+"` | corpus shape, bucketed so exact count can't fingerprint |
| `models_used_families` | `{"opus": 52, "sonnet": 3}` | population model mix — family only, no version IDs |
| `error` (only on exception) | `{"class": "OSError", "loc": "tokenmin.py:412"}` | crash signal — class name + source line, **never the message, never the path** |
| `metrics` (discovery layer) | `{"cache_hit_bucket":"low","avg_tools_per_turn_bucket":"sequential","top_tool":"Bash","window_cost_bucket":"heavy","avg_input_per_turn_bucket":"large"}` | bucketed distribution shapes so we can discover NEW optimization patterns empirically (e.g., "30% of users in low-cache + high-input bucket — likely a new detector lives there") |
| `setup_signature` (discovery layer) | `{"has_global_claude_md":false,"claude_md_size_bucket":"absent","hooks_bucket":"none","mcp_bucket":"few","custom_agents_bucket":"none","custom_skills_bucket":"none","output_style_set":false,"enable_tool_search_set":false}` | categorical features that cluster users into setup types without revealing identifiable specifics |

### What's never sent

- The snapshot itself
- File paths, project names, MCP server names — anything from `~/.claude/`
- Raw error messages, exception arguments, tracebacks beyond a class+line stub
- Your IP (the server-side endpoint, when one exists, will discard the request IP at the edge)
- Your email, GitHub handle, machine name, user name

### Inspect for yourself

```
tokenmin telemetry dry-run
```

Prints the exact JSON payload that WOULD be sent for a representative run.
No collection, no network — pure dry-run.

```
tokenmin telemetry status
```

Shows current state (on / off / unset), endpoint, env-var override, and the
install_id that would identify this install.

### Cryptographic discipline

`install_id` derives from `HMAC-SHA256(install_salt, "install-id-v1")[:16]`.
Different tag from the anonymization-hash output (`HMAC-SHA256(install_salt,
<value>)`), so the install_id can never collide with a path or label hash.
An adversary with the server-side telemetry corpus cannot reverse-derive
the salt from install_id alone (it would require a hash-extension attack
on HMAC-SHA256, which is infeasible).

### Endpoint posture

Today, no default telemetry endpoint ships. Events are formed but not
transmitted unless a user explicitly opts in and points
`telemetry_endpoint` in `~/.tokenmin/settings.json` at a URL.

When a hosted endpoint is deployed (see [ROADMAP.md](ROADMAP.md)) it will be:

- HTTPS only; HTTP rejected at the edge
- Request IP discarded at receive (not stored, not logged with the event)
- Events stored aggregated by day, not per-request
- Retention: 90 days for raw events; aggregates kept indefinitely
- Endpoint URL stored in user's `~/.tokenmin/settings.json` — change it (or set to null) anytime
- Endpoint code will be published for parity with the client trust story

## Recent hardening — v0.8 security re-scan (2026-05-23)

After shipping the install-path rewrite (v0.5), B2 client redesign (v0.5–0.7),
`tokenmin watch` (v0.6), and the engine v0.5 detectors (v0.7), we ran a
red-team pass against the new surfaces. Findings + resolutions:

| # | Surface | Finding | Resolution |
|---|---|---|---|
| R1 | Install path + auto-update | `TOKENMIN_TOKEN`-installed F&F users had their token scrubbed from `.git/config` after clone — auto-update against private repos then failed silently | Installer now writes a per-install git credential helper file (`<install>/.git-credentials`, chmod 0600) and points `credential.helper` at it. Token is no longer in `.git/config` (not visible to `git config --list`) but auto-update works. |
| R2 | Terminal renderer + `tokenmin watch` | An adversary who could write to `~/.claude/projects/<dir>` could plant ANSI escape sequences in project / file / tool names that hijacked the user's terminal (clear screen, set title, fake prompt) when rendered | New `_strip_ctl()` helper removes ANSI CSI / OSC sequences, C0 + C1 control chars (preserving tab + newline). Applied to every displayed string in `_render_terminal`, `_render_show`, and `_watch`. Property test in `tests/test_scrubber.py`. |
| R3 | `tokenmin watch` + `analyzer.py` JSONL parsers | A single multi-GB line in a planted session file would OOM Python during `for line in f` iteration | Both parsers now `readline(maxsize=1 MiB)` per line and skip files > 50 MiB outright. Bounded-discard logic skips ahead to the next newline on oversized lines. |
| R4 | `tokenmin.ai/i/<code>/` per-user invite paths | Risk that GH Pages directory listing on `/i/` would let attackers enumerate invite codes | Verified: GH Pages returns HTTP 404 on bare directories — no listing. No code change needed. |

CI test count: 13 → 14 (added `test_strip_ctl_blocks_ansi_injection`).

## Cryptographic primitives

| Use | Primitive |
|---|---|
| Identifier hashing | HMAC-SHA256, salt = 32 random bytes (per-install) [+ 32 random bytes per-run in strict mode], 64-bit truncation |
| Audit-log payload digest | SHA-256 |
| Transport | TLS (system trust store), plus commit-signature verification when `TOKENMIN_REQUIRE_SIGNED=1` |

We do not roll our own crypto. Everything above is standard `hashlib` + `hmac` from the Python stdlib.

## Known limitations

We name these so you don't have to discover them.

- **Truncated 64-bit hashes** are safe against rainbow tables (because of the salt) but theoretically vulnerable to a brute-force preimage by an adversary who already knows your salt. If your threat model includes adversaries who can read `~/.tokenmin/.salt`, you've already lost — your filesystem is compromised.
- **The user-text keyword scan reads message content in memory** before discarding it. The count of matches survives; the text does not. A truly paranoid user has to trust that the open scanner code does what it says — which is why the scanner is open.
- **A compromised maintainer GitHub account** could push a malicious commit. `TOKENMIN_REQUIRE_SIGNED=1` mitigates this if the user has the project signing key in their gpg keyring. We will publish the signing key fingerprint when the first signed release is cut.
- **`curl … | bash`** is convenient but inherently trusts the network path to GitHub. Verify-then-run is documented in the install README. We are also working on a SHA-256 publish + checksum check for the installer itself.
- **Auto-update fetches only `origin/main`** — if a user has manually pointed `origin` at a hostile fork, the update goes there. Hard to defend without changing the install model.

## What's NOT in scope

We don't claim to defend against:

- A nation-state-grade adversary on the network with control of GitHub or PyPI
- An attacker who already has code execution on your machine
- Side-channel attacks (timing, memory access patterns) against the salt
- The hosted engine being honest with your data (you trust the engine or you don't — that's the bargain)

If your threat model includes those, Tokenmin isn't your tool.
