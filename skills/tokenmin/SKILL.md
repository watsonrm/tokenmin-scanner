---
name: tokenmin
description: Analyze how the user uses Claude Code and recommend concrete improvements. Trigger when the user types bare "tokenmin" or "/tokenmin" inside Claude Code, or says "tokenmin me", "run tokenmin", "audit my claude usage", "how am I using claude code", "improve my claude setup", "what should I add to my claude config", or asks for a personalized claude code review. A bare "tokenmin" is a command — run it, don't ask what the user wants. Collects local sessions + config, anonymizes them, then hands the anonymized snapshot to the Tokenmin engine (local or hosted) for a ranked plan.
---

# Tokenmin

You are running Tokenmin. It collects the user's Claude Code usage,
anonymizes it, and produces a ranked improvement plan. Scanner and engine
both ship Apache-2.0 in this repo. See LICENSING.md.

## Steps

1. **Run the CLI if installed** — check `command -v tokenmin`. If present, just run
   `tokenmin` with no args; it scans + renders inline and is the user's expected
   "magic moment" path. Capture stdout, then skip to step 3.

2. **Otherwise locate `tokenmin.py`** — it lives in the same directory as this
   SKILL.md, OR at `~/.claude/skills/tokenmin/tokenmin.py`. Try each in order and
   run with sensible defaults:
   ```bash
   python3 <path-to-tokenmin.py> --claude-home ~/.claude --days 30 \
       --snapshot /tmp/tokenmin_snapshot.json --out /tmp/tokenmin_report.md
   ```
   - If a Tokenmin engine (local module or configured `--submit-url`) is available,
     `tokenmin.py` writes the report to `/tmp/tokenmin_report.md`. Read and summarize it.
   - If no engine is available, `tokenmin.py` writes only the anonymized snapshot and
     prints guidance. In that case, tell the user the snapshot is ready at
     `/tmp/tokenmin_snapshot.json` and that turning it into a report needs the Tokenmin
     engine — either the local engine or a hosted endpoint via `--submit-url`.
     Do NOT invent findings yourself; the open client has no rules.

3. **Summarize the output** in 4 short sections (the inline CLI output is already
   formatted; just relay it. If you ran `tokenmin.py` and got a report file, format
   it the same way):
   - Usage snapshot (one line: sessions, tokens, est. cost)
   - Top 3 friction patterns (one line each, with evidence count)
   - Top 3 recommendations (ranked)
   - Offer to implement the #1 recommendation right now

4. **If the user says yes to implementing**, open the relevant config file
   (`~/.claude/settings.json`, `~/.claude/CLAUDE.md`, etc.), make the change,
   show the diff, and ask for confirmation before saving.

## Trust rules

- The snapshot is anonymized by `anonymize.py` before it is written or sent.
  Submission happens only when the user passes `--submit-url`; without it,
  nothing leaves the machine.
- Never send the snapshot or report to any endpoint the user hasn't explicitly
  configured.
- Never quote raw transcript content in your summary — only the anonymized
  counts and patterns. The report and snapshot already contain no raw content.

## When to refuse

- If `~/.claude/projects/` is empty: tell the user Tokenmin needs Claude Code session
  history to run. Suggest they use Claude Code for a few days then re-run.
- If the user asks you to bypass anonymization for a remote send: refuse and
  explain. `--no-anonymize` is local-debug only and the client refuses to submit
  with it set.
