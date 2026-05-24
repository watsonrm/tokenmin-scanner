"""Heuristic detectors — this is where the actual advice lives.

SPDX-License-Identifier: Apache-2.0

Each pattern produces a `Finding`:
  - id: stable identifier
  - title: human-readable headline
  - evidence: short factual sentence with counts (no raw quotes)
  - savings_usd_per_month: rough estimate, may be 0
  - hours_to_implement: rough estimate
  - how_to_fix: copy-pasteable Markdown snippet (config, code, or instructions)

`score()` ranks findings by savings/effort with a tie-breaker on confidence.
"""
from __future__ import annotations

import statistics
from collections import Counter
from dataclasses import dataclass, field
from typing import Callable

from analyzer import Snapshot, SessionStats


@dataclass
class Finding:
    id: str
    title: str
    evidence: str
    how_to_fix: str
    savings_usd_per_month: float = 0.0
    hours_to_implement: float = 1.0
    confidence: float = 0.7  # 0..1
    category: str = "config"  # config | agents | hooks | workflow | hygiene
    # Pillar from the RMW Claude Code Workflow Optimizer:
    #   "1" context+config (highest ROI), "2" model routing, "3" parallelism+MCP,
    #   "4" density of expression, "hygiene" everything else.
    pillar: str = "hygiene"

    def score(self) -> float:
        # Avoid divide-by-zero; cap implausibly low effort.
        effort = max(self.hours_to_implement, 0.25)
        # Pillar 1 findings get a small priority boost — the optimizer ranks
        # them as 80% of total gains.
        pillar_boost = 1.3 if self.pillar == "1" else 1.0
        return (self.savings_usd_per_month + 5) / effort * self.confidence * pillar_boost


# --- helpers ----------------------------------------------------------------

def _overspend_evidence(snap: Snapshot, opus_cost: float, total_cost: float, opus_sessions: int) -> str:
    """Build the model_overspend evidence line.

    For users with heavy subagent workflows (scanner issue #6), tokenmin only
    sees main-session models — subagent model choices live in agent .md
    frontmatter that we don't read. Without disclosing this, an Opus-heavy
    main session reads as "everything is on Opus" even when the user has
    already routed subagents to cheaper models.
    """
    pct = 100.0 * opus_cost / total_cost
    base = (
        f"{pct:.0f}% of your model cost is on Opus "
        f"({opus_sessions} of {len(snap.sessions)} sessions). "
        f"Most workflows can run on Sonnet for ~5x less per token."
    )
    total_agent_calls = sum(sum(s.agents_used.values()) for s in snap.sessions)
    if total_agent_calls > 50:
        base += (
            f" Note: this measures your MAIN session only. "
            f"Your {total_agent_calls} Agent calls spawn subagents whose model "
            f"choices live in `.claude/agents/*.md` frontmatter — tokenmin "
            f"doesn't read those, so this finding may overstate the issue if "
            f"your subagents are already routed to Haiku/Sonnet."
        )
    return base


def _monthly_factor(snap: Snapshot) -> float:
    """Scale evidence-window numbers to a month."""
    if snap.window_days <= 0:
        return 1.0
    return 30.0 / snap.window_days


def _top_files_reread(snap: Snapshot, threshold: int = 3) -> Counter:
    """Files read >threshold times in a single session, summed across sessions."""
    c: Counter = Counter()
    for s in snap.sessions:
        for f, n in s.files_read.items():
            if n >= threshold:
                c[f] += n - 1  # only the "extra" reads are wasted
    return c


# --- detectors --------------------------------------------------------------

def detect_no_global_claude_md(snap: Snapshot) -> Finding | None:
    if snap.config.has_global_claude_md:
        return None
    if len(snap.sessions) < 5:
        return None
    # v0.12.4 (scanner#7): if the user has project-scoped CLAUDE.md files,
    # downgrade severity dramatically — they've made a defensible choice and
    # we shouldn't fire as a 95%-confidence problem. Reframe as "consider
    # promoting" instead of "you're missing it."
    cfg = snap.config
    has_project_scoped = getattr(cfg, "project_cwd_with_claude_md", 0) > 0
    project_count = getattr(cfg, "project_cwd_with_claude_md", 0)
    project_total = max(getattr(cfg, "project_cwd_total", 0), 1)
    if has_project_scoped:
        title = "No GLOBAL CLAUDE.md — consider promoting cross-project patterns"
        evidence = (
            f"You have project-level CLAUDE.md in {project_count} of {project_total} "
            f"projects, but no ~/.claude/CLAUDE.md. Any pattern you repeat across "
            f"projects (style, hooks, agent triggers) could move up to global."
        )
        confidence = 0.30
        savings = 3.0 * _monthly_factor(snap) * len(snap.sessions) / 30
    else:
        title = "No global CLAUDE.md — Claude restarts from zero in every project"
        evidence = (
            f"You've run {len(snap.sessions)} sessions but have no "
            f"~/.claude/CLAUDE.md. Claude relearns your preferences every time."
        )
        confidence = 0.95
        savings = 8.0 * _monthly_factor(snap) * len(snap.sessions) / 30
    return Finding(
        id="no_global_claude_md",
        category="config",
        pillar="1",
        title=title,
        evidence=evidence,
        savings_usd_per_month=savings,
        hours_to_implement=0.5,
        confidence=confidence,
        how_to_fix=(
            "Per the RMW Claude Code Workflow Optimizer (Pillar 1: context + "
            "config discipline — highest ROI), create `~/.claude/CLAUDE.md` "
            "and keep it under 200 lines. Cover:\n"
            "- environment commands you run constantly\n"
            "- syntax conventions and house style\n"
            "- non-negotiable architectural rules\n"
            "- pointers to where deeper detail lives\n\n"
            "Move anything project-specific to a project-level `CLAUDE.md` or "
            "path-scoped rules under `.claude/rules/`.\n"
            "Source: https://code.claude.com/docs/en/memory.md\n"
        ),
    )


def detect_no_hooks(snap: Snapshot) -> Finding | None:
    if snap.config.global_hook_count > 0:
        return None
    perm_denies = sum(s.permission_denies for s in snap.sessions)
    if perm_denies < 5 and len(snap.sessions) < 10:
        return None
    return Finding(
        id="no_hooks",
        category="hooks",
        pillar="hygiene",
        title="No hooks configured — Claude can't react to your events",
        evidence=(
            f"0 hooks in ~/.claude/settings.json across {len(snap.sessions)} sessions "
            f"and {perm_denies} permission denies."
        ),
        savings_usd_per_month=12.0,
        hours_to_implement=1.0,
        confidence=0.8,
        how_to_fix=(
            "Add a `SessionStart` hook that runs `git fetch` + `git status` + your "
            "test command, so Claude opens every session knowing repo state. "
            "Example `settings.json` fragment:\n\n"
            "```json\n"
            "{\n"
            '  "hooks": {\n'
            '    "SessionStart": [\n'
            '      { "command": "git fetch --quiet && git status -sb" }\n'
            "    ]\n"
            "  }\n"
            "}\n"
            "```\n"
        ),
    )


def detect_repeated_file_reads(snap: Snapshot) -> Finding | None:
    waste = _top_files_reread(snap)
    if not waste:
        return None
    extra_reads = sum(waste.values())
    if extra_reads < 10:
        return None
    # rough: each wasted read costs ~3K tokens × $3/M (sonnet input) = $0.009
    monthly_usd = extra_reads * 0.009 * _monthly_factor(snap)
    return Finding(
        id="repeated_file_reads",
        category="workflow",
        pillar="4",
        title="Re-reading the same files inside a single session",
        evidence=(
            f"{extra_reads} extra Read calls on {len(waste)} files in the "
            f"last {snap.window_days} days."
        ),
        savings_usd_per_month=monthly_usd,
        hours_to_implement=0.5,
        confidence=0.85,
        how_to_fix=(
            "Per Pillar 4 of the optimizer (density of expression):\n"
            "1. Reference files with `@path/to/file` instead of asking Claude "
            "to Read them — `@file` is more token-efficient and doesn't "
            "trigger a separate tool call.\n"
            "2. Pre-load high-traffic files in your project `CLAUDE.md` "
            "(e.g. \"the schema lives at src/db/schema.ts — read once at start\").\n"
            "3. Push noisy investigation into a subagent so its context never "
            "hits your main session.\n"
        ),
    )


def detect_no_custom_agents(snap: Snapshot) -> Finding | None:
    if snap.config.custom_agents:
        return None
    # Look at how often the user spawned `Agent` with general-purpose
    general_calls = sum(
        s.agents_used.get("general-purpose", 0) for s in snap.sessions
    )
    if general_calls < 5:
        return None
    # v0.12.4 (scanner#7): now that the analyzer reads each project's CWD
    # from its session JSONL, we can see project-level .claude/agents/ dirs.
    # If the user has them, downgrade from "you have none" to "consider
    # promoting" — and lower confidence so it filters as low-impact.
    cfg = snap.config
    proj_agent_dirs = getattr(cfg, "project_cwd_with_local_agents", 0)
    proj_agent_count = getattr(cfg, "project_cwd_with_agent_count", 0)
    if proj_agent_dirs > 0:
        title = "Project-scoped agents — consider promoting reusable ones to global"
        evidence = (
            f"{general_calls} general-purpose Agent calls, and you have "
            f"{proj_agent_count} project-scoped custom agents across "
            f"{proj_agent_dirs} project(s). Patterns reused across projects "
            f"belong at ~/.claude/agents/ — but a project-scoped setup is fine."
        )
        confidence = 0.35
        savings = 5.0
    else:
        title = "No global custom agents — consider promoting cross-project patterns"
        evidence = (
            f"{general_calls} general-purpose Agent calls and 0 custom agents in "
            f"the global ~/.claude/agents/ directory."
        )
        confidence = 0.6
        savings = 15.0
    return Finding(
        id="no_custom_agents",
        category="agents",
        pillar="3",
        title=title,
        evidence=evidence,
        savings_usd_per_month=savings,
        hours_to_implement=2.0,
        confidence=confidence,
        how_to_fix=(
            "Per Pillar 3 of the optimizer (subagents for noisy investigation): "
            "subagents run in their own context, so verbose work — recursive "
            "scans, large log parses, test-suite runs — never pollutes your "
            "main session. The parent only sees the structured summary.\n\n"
            "If you already use per-project agents at `<project>/.claude/agents/`: "
            "consider promoting any pattern you use across multiple projects "
            "(search, web research, slack reading, etc.) to global agents in "
            "`~/.claude/agents/` so they're available in every session, not just "
            "the one project where they live today.\n\n"
            "If you haven't built custom agents yet: identify your top 2–3 "
            "recurring agent tasks and create a `~/.claude/agents/<name>.md` for "
            "each. Manage via `/agents`.\n\n"
            "Source: https://code.claude.com/docs/en/best-practices.md\n"
        ),
    )


def detect_high_redo_signal(snap: Snapshot) -> Finding | None:
    total_user = sum(s.user_turns for s in snap.sessions)
    redos = sum(s.redo_signals for s in snap.sessions)
    if total_user < 50 or redos / max(total_user, 1) < 0.05:
        return None
    pct = 100 * redos / total_user
    return Finding(
        id="high_redo_signal",
        category="workflow",
        pillar="1",
        title="You're often telling Claude 'actually, do X instead'",
        evidence=(
            f"{redos} course-correction turns out of {total_user} user turns "
            f"({pct:.1f}%)."
        ),
        savings_usd_per_month=20.0,
        hours_to_implement=1.5,
        confidence=0.7,
        how_to_fix=(
            "Per Pillar 1 of the optimizer — verification-first workflow is "
            "the single biggest quality lever. Course-corrections mean Claude "
            "shipped plausible-but-wrong output. Break the loop:\n\n"
            "1. Write the failing test (or screenshot the bug, or state the "
            "expected log output) BEFORE asking for code.\n"
            "2. Use Plan Mode for ambiguous work — `Shift+Tab` cycles into it. "
            "Plan Mode separates 'what to build' from 'build it' and prevents "
            "cascading misunderstandings in multi-file changes.\n"
            "3. Add house-style rules to CLAUDE.md that match the things "
            "you keep correcting.\n"
        ),
    )


def detect_long_searches(snap: Snapshot) -> Finding | None:
    bursts = sum(s.long_searches for s in snap.sessions)
    if bursts < 3:
        return None
    return Finding(
        id="long_searches",
        category="workflow",
        pillar="4",
        title="Claude burns minutes hunting for files",
        evidence=(
            f"{bursts} session segments with 4+ consecutive search-tool calls "
            f"(Grep/Glob/find/Bash)."
        ),
        savings_usd_per_month=10.0,
        hours_to_implement=0.5,
        confidence=0.7,
        how_to_fix=(
            "Per Pillar 4 (density of expression — scope by coordinate): vague "
            "asks like 'look around the auth files' burn tokens on exploration. "
            "Precise asks like 'fix the session-expiration edge case in "
            "src/auth/session.ts lines 42–68' skip the search entirely.\n\n"
            "Add a 'where things live' map to your project CLAUDE.md:\n\n"
            "```md\n"
            "## Where things live\n"
            "- API routes: `src/api/`\n"
            "- Tests: `tests/<module>.test.ts`\n"
            "- Config: `config/*.yaml`\n"
            "```\n"
        ),
    )


def detect_no_mcp(snap: Snapshot) -> Finding | None:
    if snap.config.mcp_servers:
        return None
    if len(snap.sessions) < 5:
        return None
    return Finding(
        id="no_mcp",
        category="config",
        pillar="3",
        title="No MCP servers — Claude can't reach your tools natively",
        evidence="0 MCP servers configured.",
        savings_usd_per_month=5.0,
        hours_to_implement=1.0,
        confidence=0.5,  # genuinely depends on what user does
        how_to_fix=(
            "Per Pillar 3 of the optimizer: for any external service you talk "
            "to repeatedly (GitHub, Slack, databases, internal APIs), an MCP "
            "server eliminates the token cost of explaining HTTP and parsing "
            "responses. Claude calls the service natively.\n\n"
            "Pick one external system and wire its MCP server. Start with "
            "the official ones at https://github.com/modelcontextprotocol/servers.\n"
        ),
    )


def detect_model_overspend(snap: Snapshot) -> Finding | None:
    """If a lot of cheap-ish work is going through Opus, suggest Sonnet."""
    opus_cost = 0.0
    total_cost = 0.0
    opus_sessions = 0
    for s in snap.sessions:
        total_cost += s.est_cost_usd
        for m, n in s.models_used.items():
            if "opus" in m.lower():
                opus_cost += s.est_cost_usd  # over-attributes when mixed; fine for v0
                opus_sessions += 1
                break
    if total_cost < 5 or opus_cost / max(total_cost, 0.01) < 0.6:
        return None
    # Sonnet is ~5x cheaper input, ~5x cheaper output. Assume 60% of opus work
    # could be sonnet without quality loss.
    monthly = (opus_cost * 0.6 * 0.8) * _monthly_factor(snap)
    if monthly < 3:
        return None
    return Finding(
        id="model_overspend",
        category="config",
        pillar="2",
        title="A lot of your spend is on Opus — route by tier",
        evidence=_overspend_evidence(snap, opus_cost, total_cost, opus_sessions),
        savings_usd_per_month=monthly,
        hours_to_implement=0.1,
        confidence=0.55,
        how_to_fix=(
            "Per Pillar 2 of the optimizer (model routing):\n\n"
            "- **Haiku:** file exploration, repo indexing, mechanical refactors, "
            "bash one-liners. Cheapest, fastest, fine for these.\n"
            "- **Sonnet:** ~90% of work — multi-file code generation, business "
            "logic, code review. The day-to-day driver.\n"
            "- **Opus:** distributed-systems debugging, greenfield architecture, "
            "hard algorithms. Don't use it to read logs.\n\n"
            "In Claude Code: `/model claude-sonnet-4-6`. Use the named `effort` "
            "tiers (low / medium / high / xhigh / max) — `medium` is the right "
            "default for most application work.\n"
            "Source: https://code.claude.com/docs/en/quickstart.md\n"
        ),
    )


def detect_oversized_claude_md(snap: Snapshot) -> Finding | None:
    """Optimizer Pillar 1: CLAUDE.md should be < 200 lines."""
    over_global = snap.config.global_claude_md_lines > 200
    over_proj = snap.config.projects_with_oversized_claude_md
    if not over_global and over_proj == 0:
        return None
    bits = []
    if over_global:
        bits.append(f"~/.claude/CLAUDE.md is {snap.config.global_claude_md_lines} lines")
    if over_proj:
        bits.append(f"{over_proj} project CLAUDE.md file(s) exceed 200 lines")
    return Finding(
        id="oversized_claude_md",
        category="config",
        pillar="1",
        title="CLAUDE.md is over the 200-line ceiling — adherence drops past that",
        evidence="; ".join(bits) + ".",
        savings_usd_per_month=20.0,
        hours_to_implement=0.5,
        confidence=0.9,
        how_to_fix=(
            "Anthropic's official guidance: `target under 200 lines per "
            "CLAUDE.md file. Longer files consume more context and reduce "
            "adherence.` Past ~200 lines, Claude's adherence to the rules "
            "actually DROPS.\n\n"
            "Keep CLAUDE.md to: environment commands you run constantly, "
            "syntax conventions, non-negotiable architectural rules, and "
            "pointers to deeper docs. Move everything else to:\n"
            "- linked docs the model pulls on demand\n"
            "- path-scoped rules under `.claude/rules/` that load only when "
            "matching files are open\n"
            "Source: https://code.claude.com/docs/en/memory.md\n"
        ),
    )


def detect_obsolete_references(snap: Snapshot) -> Finding | None:
    """Optimizer corrections section: flag known-wrong features in user docs."""
    refs = snap.config.obsolete_references
    if not refs:
        return None
    quoted = ", ".join(f"`{r}`" for r in refs)
    return Finding(
        id="obsolete_references",
        category="hygiene",
        pillar="hygiene",
        title="Your CLAUDE.md references features that don't exist",
        evidence=f"Your global CLAUDE.md mentions: {quoted}.",
        savings_usd_per_month=2.0,
        hours_to_implement=0.1,
        confidence=0.95,
        how_to_fix=(
            "An earlier widely-circulated guide invented these. They are not "
            "real Claude Code features:\n"
            "- `.claudeignore` — does not exist. Use `.gitignore` with the "
            "  `respectGitignore` setting enabled.\n"
            "- `/effort 85` — the effort parameter takes named tiers "
            "  (`low`/`medium`/`high`/`xhigh`/`max`), not a number.\n"
            "- `claude --bare` — not a real flag.\n\n"
            "Remove these references from your CLAUDE.md so Claude doesn't "
            "try to follow nonexistent instructions.\n"
            "Source: https://github.com/watsonrm/rmwcommerce/blob/main/claude-code-optimizer.md\n"
        ),
    )


def detect_long_sessions_no_clear(snap: Snapshot) -> Finding | None:
    """Optimizer Pillar 1: long sessions without /clear bloat context."""
    if len(snap.sessions) < 5:
        return None
    user_turns = [s.user_turns for s in snap.sessions if s.user_turns > 0]
    if not user_turns:
        return None
    med = statistics.median(user_turns)
    long_sessions = sum(1 for n in user_turns if n >= 40)
    if med < 25 and long_sessions < 2:
        return None
    return Finding(
        id="long_sessions_no_clear",
        category="workflow",
        pillar="1",
        title="Long sessions without `/clear` — context bloat is silently taxing you",
        evidence=(
            f"Median session is {med:.0f} user turns; {long_sessions} "
            f"session(s) ran past 40 turns without a fresh start."
        ),
        savings_usd_per_month=25.0,
        hours_to_implement=0.25,
        confidence=0.7,
        how_to_fix=(
            "Per Pillar 1 of the optimizer — treat the context window like RAM. "
            "Anthropic's docs: `Claude's context window fills up fast, and "
            "performance degrades as it fills.`\n\n"
            "Three reflexes to build:\n"
            "1. `/context` — see what's currently using space.\n"
            "2. `/compact focus on <topic>` — run around the 50% mark, not "
            "   at the last minute. Mid-session summaries preserve more detail.\n"
            "3. `/clear` — between unrelated tasks. When you switch from a "
            "   backend route to frontend styling, run it. Don't let yesterday's "
            "   debugging pay rent on today's work.\n"
            "Source: https://code.claude.com/docs/en/best-practices.md\n"
        ),
    )


# --- v0.5 detectors (sourced from the B4 research knowledge base) -----------
#
# These five target USAGE PATTERNS rather than CONFIG GAPS. Each has a
# T1-Anthropic-sourced citation backing both the mechanism and the magnitude.
# Detection runs over data already in the snapshot (cache_*_tokens, model
# counts, tool_calls Counter, etc.) — no analyzer extension required beyond
# the small ConfigSnapshot additions for output_style + enable_tool_search.


def detect_low_cache_hit_ratio(snap: Snapshot) -> Finding | None:
    """The single most under-exploited signal per Anthropic's own engineering
    blog. Cache hit ratio = cache_read / (cache_read + cache_creation + input).
    Anthropic recommends >90% for repeated workloads; we flag below 50%."""
    cache_read = sum(s.cache_read_tokens for s in snap.sessions)
    cache_write = sum(s.cache_write_tokens for s in snap.sessions)
    input_tokens = sum(s.input_tokens for s in snap.sessions)
    denom = cache_read + cache_write + input_tokens
    if denom < 100_000:
        return None  # too little data to be meaningful
    hit_ratio = cache_read / denom
    if hit_ratio >= 0.5:
        return None
    # Wasted = (non-cache input that COULD have been cache reads if at 80%) * (input - cache rate)
    target = 0.8
    headroom_tokens = max(0, (target - hit_ratio) * denom)
    # At sonnet input rates ($3/MTok), savings if those tokens hit cache instead (0.3/MTok)
    monthly_factor = _monthly_factor(snap)
    monthly_usd = headroom_tokens * (3.0 - 0.3) / 1_000_000 * monthly_factor
    if monthly_usd < 5:
        return None
    return Finding(
        id="low_cache_hit_ratio",
        category="config",
        pillar="1",
        title=f"Cache hit ratio is {int(hit_ratio*100)}% — Anthropic's target is >90% on repeated workloads",
        evidence=(
            f"Across {len(snap.sessions)} sessions: {cache_read/1_000_000:.1f}M cache-read "
            f"tokens vs {(input_tokens+cache_write)/1_000_000:.1f}M cold-input + cache-write. "
            f"You're paying full input rate on tokens that could hit cache at 0.1×."
        ),
        savings_usd_per_month=monthly_usd,
        hours_to_implement=2.0,
        confidence=0.85,
        how_to_fix=(
            "Mark stable prefixes (system prompts, tool definitions, large reference docs, "
            "your CLAUDE.md content) with `cache_control: {\"type\": \"ephemeral\"}`. Cache hits "
            "bill at 0.1× input. Anthropic's own engineering call this 'everything' for Claude "
            "Code cost.\n\n"
            "Mechanics:\n"
            "- Put your stable content FIRST in the system/messages array; cache matches by prefix.\n"
            "- Don't insert volatile content (timestamps, user names) before stable blocks — it "
            "invalidates everything downstream.\n"
            "- Use 1-hour TTL (`{\"ttl\": \"1h\"}`) when the prefix lives longer than 10 min; "
            "pays back after 2 reads.\n\n"
            "Sources:\n"
            "- https://platform.claude.com/docs/en/build-with-claude/prompt-caching\n"
            "- https://claude.com/blog/lessons-from-building-claude-code-prompt-caching-is-everything\n"
        ),
    )


def detect_multi_model_sessions(snap: Snapshot) -> Finding | None:
    """Mid-session /model switches silently flush the cache. Anthropic's
    prompt-caching docs state every model switch reprocesses the full history
    at full input rate. opusplan users especially toggle plan mode dozens of
    times without realizing."""
    multi_model_sessions = sum(1 for s in snap.sessions if len(s.models_used) > 1)
    if multi_model_sessions < 3:
        return None
    if multi_model_sessions / max(len(snap.sessions), 1) < 0.15:
        return None
    # Rough cost: each switch reprocesses ~5K-50K context tokens at full input rate.
    # Assume conservative 10K wasted per switch, average 2 extra reprocessings per affected session.
    wasted_tokens = multi_model_sessions * 10_000 * 2
    monthly_factor = _monthly_factor(snap)
    monthly_usd = wasted_tokens * 3.0 / 1_000_000 * monthly_factor
    if monthly_usd < 5:
        return None
    return Finding(
        id="multi_model_sessions",
        category="workflow",
        pillar="1",
        title="Multiple models per session — each /model switch flushes the cache",
        evidence=(
            f"{multi_model_sessions} of {len(snap.sessions)} sessions use more than one model. "
            f"Each /model switch invalidates the cache for that session; the full history is "
            f"reprocessed at full input rate."
        ),
        savings_usd_per_month=monthly_usd,
        hours_to_implement=0.5,
        confidence=0.7,
        how_to_fix=(
            "Pick a model at session start and stay with it. Switching mid-session — including "
            "Claude Code's `/model` command and `opusplan` plan-mode toggles — fully reprocesses "
            "the context history each time, billed at full input rate.\n\n"
            "Practical rules:\n"
            "- Decide model BEFORE starting work, not as the task drifts.\n"
            "- For mixed work, accept the higher tier for the whole session (cache stays warm) "
            "rather than toggling for partial-session savings.\n"
            "- For unrelated tasks needing different models, `/clear` first; then the switch "
            "happens against an empty context.\n\n"
            "Source: https://platform.claude.com/docs/en/build-with-claude/prompt-caching\n"
        ),
    )


def detect_no_output_style(snap: Snapshot) -> Finding | None:
    """outputStyle absent in settings.json. One-line config; Anthropic
    documentation around output budgets implies 40-65% output-token reduction
    on chatty workloads when set to concise."""
    if snap.config.output_style:
        return None
    if len(snap.sessions) < 5:
        return None
    output_tokens = sum(s.output_tokens for s in snap.sessions)
    if output_tokens < 100_000:
        return None
    monthly_factor = _monthly_factor(snap)
    # Conservative 25% output reduction (low end of cited range) at sonnet output rate $15/MTok
    monthly_usd = output_tokens * 0.25 * 15.0 / 1_000_000 * monthly_factor
    if monthly_usd < 5:
        return None
    return Finding(
        id="no_output_style",
        category="config",
        pillar="4",
        title="No `outputStyle` set — Claude's default verbosity costs you on every reply",
        evidence=(
            f"{output_tokens/1_000_000:.1f}M output tokens across {len(snap.sessions)} sessions, "
            f"with no `outputStyle` set in ~/.claude/settings.json. Claude calibrates length to "
            f"perceived task complexity by default; explicit style cuts that."
        ),
        savings_usd_per_month=monthly_usd,
        hours_to_implement=0.1,
        confidence=0.65,
        how_to_fix=(
            "Add an `outputStyle` line to your global settings.json:\n\n"
            "```json\n"
            "{\n"
            '  "outputStyle": "concise"\n'
            "}\n"
            "```\n\n"
            "Available styles: `default` (current behavior), `concise` (short responses, less "
            "explanation, code-only when possible). For mostly-code workflows the savings "
            "compound — every assistant turn returns less prose for the same code.\n\n"
            "On the API side, the same effect comes from prompting explicitly: 'Result only. No "
            "explanation.' or specifying an exact output schema.\n"
        ),
    )


def detect_mcp_overflow(snap: Snapshot) -> Finding | None:
    """Many mcp__* tools loaded but ENABLE_TOOL_SEARCH not set. Anthropic
    measured 191,300 -> 122,800 token recovery with one env var."""
    mcp_tools = Counter()
    for s in snap.sessions:
        for tool_name, count in s.tool_calls.items():
            if tool_name.startswith("mcp__"):
                mcp_tools[tool_name] += count
    distinct_mcp_tools = len(mcp_tools)
    if distinct_mcp_tools < 10:
        return None
    if snap.config.enable_tool_search:
        return None
    # Rough: 70K tokens recovered per session at sonnet input rate
    sessions = len(snap.sessions)
    if sessions < 5:
        return None
    recovered_per_session = 70_000
    monthly_factor = _monthly_factor(snap)
    monthly_usd = recovered_per_session * sessions * 3.0 / 1_000_000 * monthly_factor
    if monthly_usd < 5:
        return None
    return Finding(
        id="mcp_overflow_no_tool_search",
        category="config",
        pillar="3",
        title=f"{distinct_mcp_tools} MCP tools loaded but `ENABLE_TOOL_SEARCH` is off",
        evidence=(
            f"Your session prompts load {distinct_mcp_tools} distinct MCP tool definitions. "
            f"Anthropic measured 191,300 → 122,800 token context recovery (~70K) when "
            f"`ENABLE_TOOL_SEARCH=auto` is set so Claude pulls tool definitions on demand."
        ),
        savings_usd_per_month=monthly_usd,
        hours_to_implement=0.1,
        confidence=0.8,
        how_to_fix=(
            "Add an env var to your settings.json:\n\n"
            "```json\n"
            "{\n"
            '  "env": {\n'
            '    "ENABLE_TOOL_SEARCH": "auto"\n'
            "  }\n"
            "}\n"
            "```\n\n"
            "Claude Code's runtime then loads MCP tool definitions on demand rather than dumping "
            "all of them into the system prompt. The recovered context is usable by your actual "
            "work — and the tools are still callable, just resolved lazily.\n\n"
            "Source: https://www.anthropic.com/engineering/advanced-tool-use\n"
        ),
    )


def detect_high_context_per_turn(snap: Snapshot) -> Finding | None:
    """Sessions where average input_tokens per assistant turn approaches the
    1M-context-trap threshold. Sonnet 4.5 accuracy collapses past 256K."""
    high_context_sessions = 0
    for s in snap.sessions:
        if s.assistant_turns < 3:
            continue
        avg_per_turn = s.input_tokens / s.assistant_turns
        if avg_per_turn >= 200_000:
            high_context_sessions += 1
    if high_context_sessions < 2:
        return None
    return Finding(
        id="high_context_per_turn",
        category="workflow",
        pillar="1",
        title="Some sessions are operating near the context-collapse zone",
        evidence=(
            f"{high_context_sessions} session(s) averaged 200K+ input tokens per assistant turn. "
            f"Sonnet 4.5 accuracy degrades sharply past 256K and collapses to ~18% past 500K — "
            f"you're spending Opus/Sonnet rates on context Claude can't fully use."
        ),
        savings_usd_per_month=high_context_sessions * 10.0 * _monthly_factor(snap),
        hours_to_implement=0.25,
        confidence=0.6,
        how_to_fix=(
            "Per Claude Code's context-window guidance:\n\n"
            "1. Run `/context` to see what's currently using space — usually CLAUDE.md, prior "
            "tool results, and re-loaded files dominate.\n"
            "2. Use `/compact focus on <topic>` near the 50% mark, not at the last minute.\n"
            "3. Use `/clear` between unrelated tasks — yesterday's debugging shouldn't pay rent "
            "on today's work.\n"
            "4. For genuinely large work, split into subtasks the agent can hand off to "
            "subagents (each runs in its own context).\n\n"
            "Anthropic on the 1M context window: useful in narrow cases, but accuracy degrades "
            "past 256K and falls off a cliff past 500K. Bigger context isn't free.\n"
            "Source: https://code.claude.com/docs/en/best-practices\n"
        ),
    )


# --- runner -----------------------------------------------------------------

DETECTORS: list[Callable[[Snapshot], Finding | None]] = [
    detect_no_global_claude_md,
    detect_oversized_claude_md,
    detect_obsolete_references,
    detect_long_sessions_no_clear,
    detect_no_hooks,
    detect_repeated_file_reads,
    detect_no_custom_agents,
    detect_high_redo_signal,
    detect_long_searches,
    detect_no_mcp,
    detect_model_overspend,
    # v0.5 additions
    detect_low_cache_hit_ratio,
    detect_multi_model_sessions,
    detect_no_output_style,
    detect_mcp_overflow,
    detect_high_context_per_turn,
]


def run_all(snap: Snapshot) -> list[Finding]:
    findings: list[Finding] = []
    for det in DETECTORS:
        try:
            f = det(snap)
            if f:
                findings.append(f)
        except Exception:
            # A buggy detector shouldn't take down the whole run.
            continue
    findings.sort(key=lambda x: x.score(), reverse=True)
    return findings
