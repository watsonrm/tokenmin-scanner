# Tokenmin Scanner

**This is the public, Apache-2.0 audit copy of the Tokenmin scanner** — the code
that walks your local Claude usage, anonymizes it, and decides what (if
anything) leaves your machine. If you've been invited to the Tokenmin
friends-and-family preview, this is the code you can read end-to-end *before*
you trust the bargain.

> The deal: free in exchange for an anonymized usage snapshot. The scanner
> code that decides what to send is the code in this repo. Read it, diff it
> against what your machine is actually running, then make the call.

## What's in here, what isn't

| Concern | Where it lives |
|---|---|
| Walking `~/.claude` sessions, settings, agents, skills, MCP config | This repo — [`skills/tokenmin/analyzer.py`](skills/tokenmin/analyzer.py) |
| Parsing claude.ai / Claude Desktop chat exports | This repo — [`skills/tokenmin/analyzer_chat_export.py`](skills/tokenmin/analyzer_chat_export.py) |
| Anonymization (paths, secrets, labels, identifiers) | This repo — [`skills/tokenmin/anonymize.py`](skills/tokenmin/anonymize.py) |
| The orchestrator CLI — decides whether to write a snapshot, submit it, or hand off to a local engine | This repo — [`skills/tokenmin/tokenmin.py`](skills/tokenmin/tokenmin.py) |
| Detection rule base, scoring, report rendering | **Not here.** Lives in the proprietary `watsonrm/tokenmin-core`. |
| Hosted server | **Not here.** Lives in the F&F preview bundle. |

This repo is **scanner-only**. Running it produces an anonymized snapshot. It
does not produce a report (that's the proprietary engine's job). The scanner is
fully functional without the engine — pass `--snapshot snap.json` to inspect
what would be sent, and `--out report.md` only works if an engine is also
installed.

## Install — verify, then run

The audit-first install (no `curl | bash`):

```bash
# 1. fetch the installer and its published checksum
curl -fsSL -o install.sh https://raw.githubusercontent.com/watsonrm/rmwcommerce/main/tokenmin/install.sh
curl -fsSL -o install.sh.sha256 https://raw.githubusercontent.com/watsonrm/rmwcommerce/main/tokenmin/install.sh.sha256

# 2. verify the checksum
shasum -a 256 -c install.sh.sha256

# 3. (optional but recommended) read the script before running it
less install.sh

# 4. run it — public-scanner mode, no F&F credentials needed
TOKENMIN_FF=0 bash install.sh
```

Quick path (trusts the network all the way to GitHub):

```bash
TOKENMIN_FF=0 curl -fsSL https://raw.githubusercontent.com/watsonrm/rmwcommerce/main/tokenmin/install.sh | bash
```

## Two-minute audit

After install, no network calls, no collection:

```bash
git clone https://github.com/watsonrm/tokenmin-scanner.git
cd tokenmin-scanner
./tokenmin --selfcheck
```

`--selfcheck` runs the anonymizer over a fixed set of sample inputs and prints
the scrubbed output as JSON. No collection, no network. It's the literal
demonstration of "here is what the scrubber does, on inputs designed to expose
each rule." Reading [`skills/tokenmin/anonymize.py`](skills/tokenmin/anonymize.py)
in full takes about 5 minutes.

Then run a real collection without writing anything off-machine:

```bash
./tokenmin --source code --snapshot my-snapshot.json
# inspect my-snapshot.json — that's the literal payload that would be sent
# in hosted mode.
```

## Honest naming

The output is **pseudonymized**, not strictly anonymous. Hashes are stable
across runs by default so the engine can correlate ("same file re-read 12×").
That same stability lets a determined adversary with many snapshots from the
same user fingerprint them.

For strict anonymity at the cost of cross-run correlation, set
`TOKENMIN_STRICT_ANONYMIZE=1` — hashes get salted per-run.

What stays in the snapshot, in full:

- Counts (turns, tool calls, files read, agents spawned)
- Per-session token usage + USD cost estimate
- Model names (the Anthropic product names — `claude-opus-4-7`, etc.)
- Timestamps (start/end of each session)
- Built-in tool names (Bash, Read, Edit, Agent, Grep, …)
- Built-in agent names (general-purpose)

What gets hashed (whole-string, no suffix leak):

- File paths (Read/Write/Edit `file_path` inputs)
- Project directory names
- MCP server names and `mcp__*` tool names
- Custom agent / skill / command names
- User-defined `subagent_type` values

What gets stripped:

- `/Users/<name>`, `/home/<name>`, `C:\Users\<name>` (and URL-encoded variants)
- Mangled Claude Code project paths (`-Users-<name>-...`)
- Emails, IPs, Anthropic / OpenAI / Stripe / GitHub / Slack / AWS / Google /
  npm tokens, JWTs, PEM private-key blocks, generic high-entropy strings,
  bearer tokens

What never reaches the snapshot:

- Raw message text from user prompts (read in memory for the keyword scan
  below; discarded immediately)
- Raw assistant responses
- Tool call results / outputs
- Anything outside `~/.claude/` (or, for chat-export mode, anything outside
  the export blob you point at)

The keyword scan: user-text is lowercased and matched against
`{"actually", "no wait", "instead", "undo", "revert", "scratch that",
"never mind", "wrong", "go back"}`. The *count* of matches survives; the
matched text does not.

## Transport guarantees

`tokenmin --out report.md` is 100% local. The only network call is when you
pass `--submit-url`. That code path:

- Refuses `--submit-url http://...` for non-localhost (HTTPS required).
- Refuses to combine `--submit-url` with `--no-anonymize`.
- Prefers `--api-key-env VAR` over `--api-key TOKEN` (CLI token leaks into
  `ps` and shell history).

## Auto-update is opt-in

The wrapper script can update the rule base by ff-pulling `origin/main`.
Defaults:

- `TOKENMIN_AUTOUPDATE=prompt` (default) — interactive: shows
  "update available, pull?" and skips silently on non-tty.
- `TOKENMIN_AUTOUPDATE=auto` — unattended.
- `TOKENMIN_AUTOUPDATE=off` — never check.
- `TOKENMIN_REQUIRE_SIGNED=1` — only pull commits whose GPG signature
  verifies locally.

## License

Apache-2.0. See [`LICENSE`](LICENSE) and [`NOTICE`](NOTICE). The proprietary
engine lives in `watsonrm/tokenmin-core` under a separate license.

## Repos

| Repo | Public? | What's there |
|---|---|---|
| **`watsonrm/tokenmin-scanner`** (this) | Public, Apache-2.0 | Scanner + anonymizer + CLI |
| `watsonrm/tokenmin` | Private | F&F preview bundle — vendors this scanner + engine + server, clone-and-run |
| `watsonrm/tokenmin-core` | Private | Proprietary engine + rule base |
