# Tokenmin — your Claude Code improvement plan

_Generated 2026-06-07 02:53 UTC · 30-day window · anonymized — no paths, names, secrets, or message content._

> Findings are graded against the **RMW Claude Code Workflow Optimizer**. Each one is tagged with the optimizer pillar it lives in. Pillar 1 (context + config discipline) is where ~80% of the gains live.

---

## Your Tokenmin Score: C- · 72/100

**Solid Operator** _(provisional — re-run after a week of use)_

| Pillar | Score |
|---|---|
| Context & config | `████████░░` 75/100 |
| Model routing | `██████████` 100/100 |
| Parallelism & MCP | `███████░░░` 70/100 |
| Density | `████████░░` 85/100 |

_Share your score: `tokenmin share`._

---

## TL;DR

- **7 improvement(s)** identified, est. **~95% quota** stretch on your flat-fee plan.
  _You pay flat, so these don't lower your bill — they let you do more Claude work within the same cap._
- **Total effort to implement everything:** ~4.2 hrs.
- **Start here:** 4 MCP servers configured with tool search disabled — full schema cost on every turn (~95% quota, 0.1 hrs).

## Usage snapshot

| Metric | Value |
|---|---|
| Sessions analyzed | 3 |
| Total user turns | 49 |
| Total assistant turns | 49 |
| Avg tools per assistant turn | 2.4 |
| Input tokens | 213K |
| Output tokens | 12K |
| Billing plan | Claude Max (flat) |
| _Plan note_ | _You pay flat — savings are shown as quota stretch + tokens saved, not dollars. Run `tokenmin show --raw` for API-equivalent numbers._ |
| Models used | Opus 88%, Sonnet 12% |
| Top tools | Grep 58%, Agent 22%, Read 12%, Bash 9% |

## Your setup

| Item | Status |
|---|---|
| Global `~/.claude/CLAUDE.md` | present |
| Global `settings.json` | MISSING |
| Hooks configured | 0 |
| Permission rules | 0 |
| Custom agents | 0 |
| Custom skills | 0 |
| Slash commands | 0 |
| MCP servers | 4 |
| Projects with project-level CLAUDE.md | 0 / 2 |

## Pillar distribution of your findings

| Pillar | Findings | Optimizer practice |
|---|---|---|
| **1** | 1 | Context + config discipline (highest ROI) |
| **3** | 2 | Parallelism, subagents, MCP |
| **4** | 1 | Density of expression |
| **hygiene** | 3 | Other hygiene |

## Recommendations (ranked)

### 1. 4 MCP servers configured with tool search disabled — full schema cost on every turn  <sub>·  ~95% quota  ·  0.1 hrs  ·  conf 75%  ·  [Pillar 3]</sub>

**Evidence:** You have 4 MCP server(s) configured and `ENABLE_TOOL_SEARCH` is not enabled. All tool schemas load upfront on every turn. On Vertex AI or behind a non-first-party `ANTHROPIC_BASE_URL` proxy, this flag is disabled by default.

**How to fix:**

Set `ENABLE_TOOL_SEARCH=auto` in your shell environment (or the settings.json env block) so Claude only loads tool schemas as needed:

```bash
export ENABLE_TOOL_SEARCH=auto
```

Anthropic measured ~70K context tokens recovered on a heavy-MCP setup (191,300 → 122,800 tokens per turn). If you're on Vertex or a proxy, you must set this explicitly — it's disabled by default in those environments because most proxies don't forward `tool_reference` blocks.
Source: https://code.claude.com/docs/en/mcp

### 2. CLAUDE.md is over the 200-line ceiling — adherence drops past that  <sub>·  ~95% quota  ·  0.5 hrs  ·  conf 90%  ·  [Pillar 1]</sub>

**Evidence:** ~/.claude/CLAUDE.md is 226 lines.

**How to fix:**

Anthropic's official guidance: `target under 200 lines per CLAUDE.md file. Longer files consume more context and reduce adherence.` Past ~200 lines, Claude's adherence to the rules actually DROPS.

Keep CLAUDE.md to: environment commands you run constantly, syntax conventions, non-negotiable architectural rules, and pointers to deeper docs. Move everything else to:
- linked docs the model pulls on demand
- path-scoped rules under `.claude/rules/` that load only when matching files are open
Source: https://code.claude.com/docs/en/memory.md

### 3. 1 repeatedly-denied pattern(s) — candidates for a `permissions.deny` rule  <sub>·  ~95% quota  ·  0.1 hrs  ·  conf 70%  ·  [Hygiene]</sub>

**Evidence:** The following patterns were denied 5+ times across the window:
  - `Bash` denied 6 time(s)
Each denial costs an assistant turn plus a user permission prompt — and the pattern is not in `permissions.deny`, so Claude keeps trying.

**How to fix:**

Add the patterns to your `~/.claude/settings.json` `permissions` block so Claude stops trying:

```json
{
  "permissions": {
    "deny": [
      "Bash(rm -rf:*)",
      "Write(/etc/**)"
    ]
  }
}
```

The mirror move: patterns you've always approved (10+ times) belong in `allow` — Anthropic's best-practices doc notes that by the tenth approval you're not really reviewing.
Source: https://code.claude.com/docs/en/best-practices

### 4. Your CLAUDE.md references features that don't exist  <sub>·  ~50% quota  ·  0.1 hrs  ·  conf 95%  ·  [Hygiene]</sub>

**Evidence:** Your global CLAUDE.md mentions: `.claudeignore`, `/effort 85`, `claude --bare`.

**How to fix:**

An earlier widely-circulated guide invented these. They are not real Claude Code features:
- `.claudeignore` — does not exist. Use `.gitignore` with the   `respectGitignore` setting enabled.
- `/effort 85` — the effort parameter takes named tiers   (`low`/`medium`/`high`/`xhigh`/`max`), not a number.
- `claude --bare` — not a real flag.

Remove these references from your CLAUDE.md so Claude doesn't try to follow nonexistent instructions.
Source: https://github.com/watsonrm/rmwcommerce/blob/main/claude-code-optimizer.md

### 5. Claude burns minutes hunting for files  <sub>·  ~95% quota  ·  0.5 hrs  ·  conf 70%  ·  [Pillar 4]</sub>

**Evidence:** 19 session segments with 4+ consecutive search-tool calls (Grep/Glob/find/Bash).

**How to fix:**

Per Pillar 4 (density of expression — scope by coordinate): vague asks like 'look around the auth files' burn tokens on exploration. Precise asks like 'fix the session-expiration edge case in src/auth/session.ts lines 42–68' skip the search entirely.

Add a 'where things live' map to your project CLAUDE.md:

```md
## Where things live
- API routes: `src/api/`
- Tests: `tests/<module>.test.ts`
- Config: `config/*.yaml`
```

### 6. No hooks configured — Claude can't react to your events  <sub>·  ~95% quota  ·  1.0 hrs  ·  conf 80%  ·  [Hygiene]</sub>

**Evidence:** 0 hooks in ~/.claude/settings.json across 3 sessions and 6 permission denies.

**How to fix:**

Add a `SessionStart` hook that runs `git fetch` + `git status` + your test command, so Claude opens every session knowing repo state. Example `settings.json` fragment:

```json
{
  "hooks": {
    "SessionStart": [
      { "command": "git fetch --quiet && git status -sb" }
    ]
  }
}
```

### 7. No global custom agents — consider promoting cross-project patterns  <sub>·  ~95% quota  ·  2.0 hrs  ·  conf 60%  ·  [Pillar 3]</sub>

**Evidence:** 15 general-purpose Agent calls and 0 custom agents in the global ~/.claude/agents/ directory.

**How to fix:**

Per Pillar 3 of the optimizer (subagents for noisy investigation): subagents run in their own context, so verbose work — recursive scans, large log parses, test-suite runs — never pollutes your main session. The parent only sees the structured summary.

If you already use per-project agents at `<project>/.claude/agents/`: consider promoting any pattern you use across multiple projects (search, web research, slack reading, etc.) to global agents in `~/.claude/agents/` so they're available in every session, not just the one project where they live today.

If you haven't built custom agents yet: identify your top 2–3 recurring agent tasks and create a `~/.claude/agents/<name>.md` for each. Manage via `/agents`.

Source: https://code.claude.com/docs/en/best-practices.md

---

## Methodology + caveats

- **Source data:** `~/.claude/projects/*/*.jsonl` (Claude Code session transcripts) + `~/.claude/settings.json`, `CLAUDE.md`, `agents/`, `skills/`, `commands/`, and any local MCP config.
- **Anonymization:** all paths hashed, emails/IPs/keys scrubbed, user-home replaced with `<USER>`. The Markdown report contains no raw transcript content.
- **Cost estimates** use rough USD/Mtoken rates and assume conservative savings. Treat as order-of-magnitude, not invoice-accurate.

## Sources & attribution

Tokenmin applies the **RMW Claude Code Workflow Optimizer** ([github.com/watsonrm/rmwcommerce](https://github.com/watsonrm/rmwcommerce/blob/main/claude-code-optimizer.md)) to your local usage data. The optimizer's prescriptions come from Anthropic's official documentation and public talks by Boris Cherny (creator and head of Claude Code at Anthropic).

- Claude Code best practices — https://code.claude.com/docs/en/best-practices.md
- CLAUDE.md / memory — https://code.claude.com/docs/en/memory.md
- Quickstart — https://code.claude.com/docs/en/quickstart.md
- Effort parameter — https://platform.claude.com/docs/en/build-with-claude/effort.md
- Anthropic Engineering blog — https://www.anthropic.com/engineering/claude-code-best-practices
