---
name: tokenmin
description: Analyze how the user uses Claude Code and recommend concrete improvements. Trigger when the user says "/tokenmin", "tokenmin me", "how am I using claude code", "improve my claude setup", "what should I add to my claude config", or asks for a personalized claude code review. Collects local sessions + config, anonymizes them, then hands the anonymized snapshot to the Tokenmin engine (local or hosted) for a ranked plan.
---

# Tokenmin (open client)

You are running the Tokenmin open client. It collects the user's Claude Code usage,
anonymizes it, and hands the anonymized snapshot to the Tokenmin engine. The client
holds no detection rules — findings come from the Tokenmin engine (a local
proprietary module if installed, otherwise the hosted service). See LICENSING.md.

## Steps

1. **Locate** `tokenmin.py` — it lives in the same directory as this SKILL.md, OR at
   `~/.claude/skills/tokenmin/tokenmin.py`, OR the user may have installed it as a CLI.
   Try each in order.

2. **Run the client** with sensible defaults:
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

3. **If a report was produced, summarize** it in 4 short sections:
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
