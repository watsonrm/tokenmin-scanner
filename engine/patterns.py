"""Heuristic detectors — this is where the actual advice lives.

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

import re as _re
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
    """If a lot of cheap-ish work is going through Opus, suggest Sonnet.

    v0.12.8 (scanner#9): context-aware ranking. A user with substantial
    subagent activity has already separated cheap work from judgment work
    at the .claude/agents/ layer; firing this finding at $$$$ critical for
    them is wrong. Downgrade confidence (which drops it to low-impact) when
    subagent volume is heavy, AND emit a positive `well_routed_subagents`
    finding so the audit acknowledges the discipline rather than scolding
    a user who's already done the work.
    """
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
    # Context-aware downgrade. Two signals say the user has already routed:
    #   1. High volume of Agent tool calls (>100 across the window)
    #   2. Custom agents are defined AND most carry an explicit model: line
    # When either fires strongly, drop confidence to 0.20 so the finding
    # filters into the low-impact bucket. The disclaimer in evidence text
    # already explains the gap; this stops the bad RANKING regression.
    total_agent_calls = sum(sum(s.agents_used.values()) for s in snap.sessions)
    has_subagent_discipline = (
        total_agent_calls > 100
        or getattr(snap.config, "project_cwd_with_local_agents", 0) > 0
    )
    confidence = 0.20 if has_subagent_discipline else 0.55
    title = (
        "Main session is Opus-heavy — your subagent routing may already cover this"
        if has_subagent_discipline
        else "A lot of your spend is on Opus — route by tier"
    )
    return Finding(
        id="model_overspend",
        category="config",
        pillar="2",
        title=title,
        evidence=_overspend_evidence(snap, opus_cost, total_cost, opus_sessions),
        savings_usd_per_month=monthly,
        hours_to_implement=0.1,
        confidence=confidence,
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


def detect_well_routed_subagents(snap: Snapshot) -> Finding | None:
    """Positive informational finding — fires when the user has clearly already
    separated cheap work from judgment work at the subagent layer. Counter-
    balances the model_overspend false-alarm for sophisticated users.

    Signal: substantial Agent tool-call volume AND custom agents defined with
    explicit model: frontmatter on most of them.
    """
    total_agent_calls = sum(sum(s.agents_used.values()) for s in snap.sessions)
    if total_agent_calls < 100:
        return None
    agent_files = snap.config.custom_agents or []
    project_agents = getattr(snap.config, "project_cwd_with_local_agents", 0)
    if not agent_files and project_agents == 0:
        return None
    # Counter the model_overspend finding so the audit acknowledges the work
    # the user has already done. Zero dollar savings (this isn't a "fix"; it's
    # a confirmation), low effort (none).
    return Finding(
        id="well_routed_subagents",
        category="agents",
        pillar="2",
        title=f"Subagent routing is doing the cheap-tier work ({total_agent_calls} Agent call(s) in window)",
        evidence=(
            f"You spawned subagents {total_agent_calls} time(s) across the window. "
            f"The cheap-routing wins for most workloads happen at this layer, "
            f"not in the main session. If the model_overspend detector also "
            f"fired, it's likely overstated for your setup."
        ),
        savings_usd_per_month=0.0,
        hours_to_implement=0.0,
        confidence=0.85,
        how_to_fix=(
            "No action needed — this is an informational acknowledgment that "
            "your subagent layer is carrying the cheap-tier routing burden. "
            "If you want to push further, audit each agent file's `model:` "
            "frontmatter and confirm read-heavy agents (research, search, "
            "summarization) are on Haiku.\n"
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

# ============================================================================
# v0.12.6 detectors — 11 candidates from scanner#15, the "Strong yes" + "Yes"
# tier. Each maps to a specific scanner#15 entry; see that issue for the source
# citation and design rationale.
# ============================================================================


def detect_mcp_zombie_servers(snap: Snapshot) -> Finding | None:
    """scanner#15 #1: MCP server configured but never invoked.

    Each idle MCP server still injects its tool schema into context on every
    turn. The behavioral question — "have you fired a tool from this server
    in N days?" — is the right audit, not "do I need this server in theory?"
    """
    servers = snap.config.mcp_servers or []
    if not servers:
        return None
    # Aggregate tool calls across all sessions.
    all_calls: Counter = Counter()
    for s in snap.sessions:
        all_calls.update(s.tool_calls)
    zombies: list[str] = []
    for server in servers:
        prefix = f"mcp__{server}__"
        invocations = sum(n for name, n in all_calls.items() if name.startswith(prefix))
        if invocations == 0:
            zombies.append(server)
    if not zombies:
        return None
    if len(snap.sessions) < 10:
        return None  # too thin a window to call a server "never invoked"
    # Estimated savings — conservative: 5K context tokens per idle server per turn.
    avg_turns = sum(s.assistant_turns for s in snap.sessions) / max(len(snap.sessions), 1)
    monthly_tokens = 5_000 * len(zombies) * avg_turns * len(snap.sessions) * _monthly_factor(snap)
    monthly_usd = monthly_tokens * 0.30 / 1_000_000  # Sonnet input rate as a baseline
    z_list = ", ".join(f"`{z}`" for z in zombies[:5])
    if len(zombies) > 5:
        z_list += f", and {len(zombies) - 5} more"
    return Finding(
        id="mcp_zombie_servers",
        category="config",
        pillar="3",
        title=f"{len(zombies)} MCP server(s) configured but never invoked — schema cost for nothing",
        evidence=(
            f"Across {len(snap.sessions)} sessions, no tool from {z_list} was ever "
            f"called. Each idle server still injects its full tool schema into "
            f"the system prompt on every turn."
        ),
        savings_usd_per_month=max(monthly_usd, 8.0),
        hours_to_implement=0.1,
        confidence=0.85,
        how_to_fix=(
            "Two options:\n\n"
            "- **Remove the server** from `~/.claude/settings.json` (or "
            "wherever you configured it). One line of JSON.\n"
            "- **Keep it but defer-load**: set `ENABLE_TOOL_SEARCH=auto` in "
            "your env so Claude only loads schemas when relevant. Most useful "
            "when you have many MCP servers and use each occasionally.\n\n"
            "Audit by behavior, not by intent: \"have I fired a tool from this "
            "server in the last 30 days?\" is the question.\n"
        ),
    )


def detect_parallel_tools_underused(snap: Snapshot) -> Finding | None:
    """scanner#15 #4: sequential tool calls that should be parallel batches.

    Claude 4+ supports parallel tool calls natively. Mean tools-per-tool-turn
    should be > 1.0 when the work is parallelizable.
    """
    candidates = []  # sessions where a parallel-friendly workload looks sequential
    for s in snap.sessions:
        heavy = (s.tool_calls.get("Read", 0) + s.tool_calls.get("Grep", 0)
                 + s.tool_calls.get("Glob", 0) + s.tool_calls.get("Bash", 0))
        if heavy < 8 or not s.tools_per_turn:
            continue
        mean_tpt = sum(s.tools_per_turn) / len(s.tools_per_turn)
        if mean_tpt < 1.3:
            candidates.append((s, mean_tpt, heavy))
    if len(candidates) < 3:
        return None
    avg_mean = sum(c[1] for c in candidates) / len(candidates)
    # Anthropic measures ~90% time reduction from parallel tool use on research
    # workflows. The token savings are smaller — we estimate 15% of input tokens
    # on the affected sessions (less re-fetched context).
    affected_input = sum(c[0].input_tokens for c in candidates)
    monthly_usd = affected_input * 0.15 * 0.30 / 1_000_000 * _monthly_factor(snap)
    return Finding(
        id="parallel_tools_underused",
        category="workflow",
        pillar="3",
        title="Sequential tool calls when parallel would work — leaving speed on the table",
        evidence=(
            f"{len(candidates)} session(s) with 8+ Read/Grep/Glob/Bash calls but a mean "
            f"of only {avg_mean:.1f} tool(s) per assistant turn. Anthropic's parallel-tool-use "
            f"docs note this metric should be >1.0 when parallel calling is enabled."
        ),
        savings_usd_per_month=max(monthly_usd, 6.0),
        hours_to_implement=0.25,
        confidence=0.6,
        how_to_fix=(
            "Two interventions:\n\n"
            "- **Prompt for batching**: add to your CLAUDE.md or session-start "
            "instructions: \"When you need to read multiple files, batch the "
            "Read calls in a single response rather than reading one at a time.\"\n"
            "- **Verify parallel tool use is on**: this is the default for Claude "
            "4+ and Claude Code. If you're on the API, set "
            "`disable_parallel_tool_use: false` (the default).\n\n"
            "Anthropic measured up to 90% time reduction on research-style "
            "workloads when parallel tool calls land.\n"
            "Source: https://platform.claude.com/docs/en/agents-and-tools/tool-use/parallel-tool-use\n"
        ),
    )


def detect_opus_for_subagents(snap: Snapshot) -> Finding | None:
    """scanner#15 #5: subagents without explicit `model:` inheriting Opus.

    Subagents defined without a model field default to the parent session's
    model. If you spawn them from an Opus session, every read-heavy subagent
    runs on Opus too — for work Haiku does fine.
    """
    cfg = snap.config
    agent_files = cfg.custom_agents or []
    if not agent_files:
        return None
    # Check usage volume first — if no agents got invoked, the detector is silent.
    total_agent_calls = sum(sum(s.agents_used.values()) for s in snap.sessions)
    if total_agent_calls < 5:
        return None
    # Are any main sessions running Opus?
    opus_sessions = 0
    for s in snap.sessions:
        for m, n in s.models_used.items():
            if "opus" in m.lower():
                opus_sessions += 1
                break
    if opus_sessions == 0:
        return None
    # Parse frontmatter of each agent file to find those without `model:`.
    inheriting = []
    for path in agent_files:
        try:
            from pathlib import Path as _P
            content = _P(path).read_text(encoding="utf-8", errors="replace")[:2000]
        except OSError:
            continue
        if not content.startswith("---"):
            continue
        end = content.find("---", 3)
        if end < 0:
            continue
        frontmatter = content[3:end]
        if not _re.search(r"^model\s*:", frontmatter, _re.MULTILINE):
            from pathlib import Path as _P
            inheriting.append(_P(path).name)
    if not inheriting:
        return None
    monthly_usd = 30.0 * len(inheriting)  # rough — each inheriting agent worth ~$30/mo
    listed = ", ".join(f"`{n}`" for n in inheriting[:5])
    if len(inheriting) > 5:
        listed += f", and {len(inheriting) - 5} more"
    return Finding(
        id="opus_for_subagents",
        category="agents",
        pillar="2",
        title=f"{len(inheriting)} subagent(s) without explicit `model:` — inheriting from main session",
        evidence=(
            f"Agents without a `model:` field in their frontmatter inherit the "
            f"parent session's model. You spawned subagents {total_agent_calls} time(s) "
            f"from Opus sessions in the window. Inheriting agents: {listed}."
        ),
        savings_usd_per_month=monthly_usd,
        hours_to_implement=0.2,
        confidence=0.7,
        how_to_fix=(
            "Add an explicit `model:` to each subagent's frontmatter:\n\n"
            "```yaml\n"
            "---\n"
            "name: research-subagent\n"
            "description: ...\n"
            "model: haiku   # or sonnet for coordination/composition agents\n"
            "tools: Read, Grep, Glob\n"
            "---\n"
            "```\n\n"
            "Anthropic's guidance: use Haiku for file exploration / mechanical "
            "summarization, Sonnet for coordination, Opus for complex reasoning "
            "the subagent genuinely needs.\n"
            "Source: https://code.claude.com/docs/en/sub-agents\n"
        ),
    )


def detect_subagent_avoidance_on_huge_context(snap: Snapshot) -> Finding | None:
    """scanner#15 #6: heavy investigation in the main thread, zero Agent calls.

    The inverse of no_custom_agents. If a session does 15+ Read/Grep/Glob in the
    main thread without spawning a single Agent, it bloated the main context.
    """
    avoidance_sessions = 0
    for s in snap.sessions:
        heavy = (s.tool_calls.get("Read", 0) + s.tool_calls.get("Grep", 0)
                 + s.tool_calls.get("Glob", 0))
        if heavy < 15:
            continue
        if sum(s.agents_used.values()) > 0:
            continue
        if s.assistant_turns == 0:
            continue
        avg_input_per_turn = s.input_tokens / s.assistant_turns
        if avg_input_per_turn > 50_000:
            avoidance_sessions += 1
    if avoidance_sessions < 3:
        return None
    monthly_usd = avoidance_sessions * 8.0 * _monthly_factor(snap)
    return Finding(
        id="subagent_avoidance_on_huge_context",
        category="workflow",
        pillar="1",
        title=f"{avoidance_sessions} heavy-investigation session(s) ran in the main thread instead of a subagent",
        evidence=(
            f"{avoidance_sessions} session(s) had 15+ Read/Grep/Glob calls "
            f"with zero Agent invocations and >50K avg input tokens per turn. "
            f"Heavy investigation in the main context pollutes every subsequent "
            f"turn's cache."
        ),
        savings_usd_per_month=monthly_usd,
        hours_to_implement=0.5,
        confidence=0.65,
        how_to_fix=(
            "When you need to investigate something (recursive scans, large log "
            "parsing, repo-wide searches), spawn a subagent and let it return a "
            "structured summary — your main context never sees the noise.\n\n"
            "Inline prompt: \"Use a subagent to investigate X; report back with "
            "a summary of findings and the 3 most relevant file references.\"\n\n"
            "Or define a `research-subagent` in `.claude/agents/` that auto-routes "
            "to Haiku for the read-heavy work.\n"
            "Source: https://code.claude.com/docs/en/best-practices\n"
        ),
    )


def detect_tool_search_off_with_many_servers(snap: Snapshot) -> Finding | None:
    """scanner#15 #15: many MCP servers + tool search disabled = silent schema bloat."""
    cfg = snap.config
    n_servers = len(cfg.mcp_servers or [])
    if n_servers < 4:
        return None
    ets = cfg.enable_tool_search
    if ets and str(ets).lower() not in ("false", "0", "no", "off"):
        return None
    # If we know ENABLE_TOOL_SEARCH is "auto" or "true", we're fine.
    if ets and str(ets).lower() in ("auto", "true", "1", "yes", "on"):
        return None
    return Finding(
        id="tool_search_off_with_many_servers",
        category="config",
        pillar="3",
        title=f"{n_servers} MCP servers configured with tool search disabled — full schema cost on every turn",
        evidence=(
            f"You have {n_servers} MCP server(s) configured and `ENABLE_TOOL_SEARCH` "
            f"is not enabled. All tool schemas load upfront on every turn. On Vertex AI "
            f"or behind a non-first-party `ANTHROPIC_BASE_URL` proxy, this flag is "
            f"disabled by default."
        ),
        savings_usd_per_month=25.0,
        hours_to_implement=0.05,
        confidence=0.75,
        how_to_fix=(
            "Set `ENABLE_TOOL_SEARCH=auto` in your shell environment (or the "
            "settings.json env block) so Claude only loads tool schemas as needed:\n\n"
            "```bash\n"
            "export ENABLE_TOOL_SEARCH=auto\n"
            "```\n\n"
            "Anthropic measured ~70K context tokens recovered on a heavy-MCP setup "
            "(191,300 → 122,800 tokens per turn). If you're on Vertex or a proxy, "
            "you must set this explicitly — it's disabled by default in those "
            "environments because most proxies don't forward `tool_reference` blocks.\n"
            "Source: https://code.claude.com/docs/en/mcp\n"
        ),
    )


def detect_bash_cat_instead_of_read(snap: Snapshot) -> Finding | None:
    """scanner#15 #2: Claude shelling out to cat/ls/head/etc via Bash instead of Read/Glob/Grep."""
    total_bash_file = sum(s.bash_file_ops for s in snap.sessions)
    if total_bash_file < 10:
        return None
    total_file_ops = 0
    for s in snap.sessions:
        total_file_ops += (s.tool_calls.get("Read", 0) + s.tool_calls.get("Glob", 0)
                           + s.tool_calls.get("Grep", 0) + s.bash_file_ops)
    if total_file_ops == 0:
        return None
    ratio = total_bash_file / total_file_ops
    if ratio < 0.15:
        return None
    # Each bash file op = ~3K context tokens of unstructured output that didn't
    # need to enter context. At Sonnet input rate.
    monthly_usd = total_bash_file * 3_000 * 0.30 / 1_000_000 * _monthly_factor(snap)
    return Finding(
        id="bash_cat_instead_of_read",
        category="workflow",
        pillar="4",
        title=f"Bash file-read commands instead of Read/Glob/Grep — {int(ratio*100)}% of file operations",
        evidence=(
            f"Claude ran `cat`/`ls`/`head`/`tail`/`sed`/`awk`/`grep`/`find` via Bash "
            f"{total_bash_file} time(s) — {int(ratio*100)}% of all file operations in "
            f"the window. These return unstructured output, trigger permission prompts, "
            f"and pollute context."
        ),
        savings_usd_per_month=max(monthly_usd, 12.0),
        hours_to_implement=0.25,
        confidence=0.7,
        how_to_fix=(
            "Add a `PreToolUse` hook that intercepts `Bash` with these patterns and "
            "either blocks or warns. Example fragment for `~/.claude/settings.json`:\n\n"
            "```json\n"
            "{\n"
            "  \"hooks\": {\n"
            "    \"PreToolUse\": [\n"
            "      {\n"
            "        \"matcher\": \"Bash\",\n"
            "        \"command\": \"jq -r .input.command | grep -qE '^(cat|ls|head|tail|sed|awk|grep|find) ' && echo 'Use Read/Glob/Grep tools instead' >&2 && exit 2 || true\"\n"
            "      }\n"
            "    ]\n"
            "  }\n"
            "}\n"
            "```\n"
            "Or just add to CLAUDE.md: \"Never use bash cat/ls/head/tail/grep/find for "
            "file operations — use Read, Glob, or Grep tools.\"\n"
        ),
    )


def detect_cache_thrash_short_gaps(snap: Snapshot) -> Finding | None:
    """scanner#15 #3: sessions resumed past 5-min cache TTL paying full write cost."""
    total_thrash = sum(s.cache_thrash_events for s in snap.sessions)
    if total_thrash < 10:
        return None
    monthly_thrash = total_thrash * _monthly_factor(snap)
    if monthly_thrash < 20:
        return None
    # Each thrash event = ~50K cache_creation tokens paid at write rate (~1.25x input).
    # Saving = those tokens at read rate instead.
    avg_creation = sum(s.cache_write_tokens for s in snap.sessions) / max(1, total_thrash * 3)
    monthly_usd = monthly_thrash * avg_creation * (3.75 - 0.30) / 1_000_000  # sonnet write vs read
    return Finding(
        id="cache_thrash_short_gaps",
        category="workflow",
        pillar="4",
        title=f"Cache thrash on resumed sessions — {int(monthly_thrash)} events/month past the 5-min TTL",
        evidence=(
            f"{total_thrash} assistant turn(s) in the window resumed 5-55 min after "
            f"the previous turn and paid full cache-creation cost. Default cache TTL "
            f"is 5 minutes; gaps past that re-pay the write rate (~1.25x input) "
            f"instead of the read rate (~0.1x)."
        ),
        savings_usd_per_month=max(monthly_usd, 10.0),
        hours_to_implement=0.5,
        confidence=0.55,
        how_to_fix=(
            "Two patterns:\n\n"
            "- **Use `/clear` more deliberately**: between unrelated tasks, "
            "`/clear` is cheaper than letting the cache rebuild on next message.\n"
            "- **API users — opt into the 1h TTL**: when your workflow has natural "
            "long pauses (research, document review), set `cache_control` to use the "
            "1-hour cache. Costs 2x to write but amortizes over a much longer window. "
            "Use the beta header `extended-cache-ttl-2025-04-11` on the Messages API.\n"
            "Source: https://platform.claude.com/docs/en/build-with-claude/prompt-caching\n"
        ),
    )


def detect_effort_high_for_trivial_work(snap: Snapshot) -> Finding | None:
    """scanner#15 #8: extended thinking budget burned on turns that don't need it."""
    total_bloat = sum(s.thinking_bloat_turns for s in snap.sessions)
    total_turns = sum(s.assistant_turns for s in snap.sessions)
    if total_turns == 0:
        return None
    ratio = total_bloat / total_turns
    if ratio < 0.10 or total_bloat < 5:
        return None
    # Each bloat turn = ~8K wasted output tokens at output rate.
    monthly_usd = total_bloat * 8_000 * 15.0 / 1_000_000 * _monthly_factor(snap)
    return Finding(
        id="effort_high_for_trivial_work",
        category="workflow",
        pillar="2",
        title=f"Extended thinking burned on trivial turns — {int(ratio*100)}% of assistant turns",
        evidence=(
            f"{total_bloat} of {total_turns} assistant turn(s) produced >8K output "
            f"tokens with <=1 Edit/Write, no Agent call, and <500 visible characters. "
            f"That signature usually means the thinking-token budget was burned on a "
            f"turn that did not need deep reasoning."
        ),
        savings_usd_per_month=max(monthly_usd, 15.0),
        hours_to_implement=0.1,
        confidence=0.55,
        how_to_fix=(
            "Options in order of effort:\n\n"
            "- **Lower the effort tier for routine work**: `/effort low` or "
            "`/effort medium` at the start of a routine session.\n"
            "- **Cap the thinking budget**: set `MAX_THINKING_TOKENS=8000` in your "
            "shell environment. Useful when most of your work is plumbing and you "
            "want the option to escalate per-session rather than pay the default.\n"
            "- **On the API**: pass `thinking: {budget_tokens: 8000}` or skip the "
            "thinking parameter entirely for simple turns. Extended thinking is "
            "billed as output tokens, so the savings are direct.\n"
            "Source: https://code.claude.com/docs/en/costs\n"
        ),
    )


def detect_hook_token_burner(snap: Snapshot) -> Finding | None:
    """scanner#15 #9: user-defined hook that bloats context every fire.

    HEURISTIC — detection identifies inline hook output via known Claude Code
    wrapper tags (<local-command-stdout>, etc.). The hook event schema in JSONL
    has not been formally verified, so this fires with reduced confidence.
    """
    total_chars = sum(s.hook_event_chars for s in snap.sessions)
    total_fires = sum(s.hook_event_fires for s in snap.sessions)
    if total_fires < 10:
        return None
    avg_chars = total_chars / total_fires
    if avg_chars < 2000:
        return None
    # Each char ~ 0.25 tokens. Wasted tokens = total_chars * 0.25 at input rate.
    monthly_usd = total_chars * 0.25 * 0.30 / 1_000_000 * _monthly_factor(snap)
    return Finding(
        id="hook_token_burner",
        category="hooks",
        pillar="hygiene",
        title=f"Hook output is bloating context — avg {int(avg_chars)} chars per fire",
        evidence=(
            f"Detected {total_fires} hook output event(s) totaling {total_chars:,} "
            f"characters in the window (heuristic — based on inline Claude Code "
            f"`<local-command-stdout>` tag matching). Hook output enters context on "
            f"every fire."
        ),
        savings_usd_per_month=max(monthly_usd, 5.0),
        hours_to_implement=0.5,
        confidence=0.4,
        how_to_fix=(
            "Audit your hooks in `~/.claude/settings.json`:\n\n"
            "- **SessionStart hooks** are the biggest risk — they fire on every "
            "new session and their output enters context immediately.\n"
            "- **Filter at the hook**: instead of running `git log` (potentially "
            "thousands of lines), run `git log --oneline -10` and pipe to a "
            "specific grep.\n"
            "- **Move the work into a tool, not a hook**: if Claude needs the "
            "data sometimes, expose it as an MCP tool rather than injecting it "
            "into every turn.\n"
            "Source: https://code.claude.com/docs/en/costs\n"
        ),
    )


def detect_permission_denies_loop(snap: Snapshot) -> Finding | None:
    """scanner#15 #13: same tool/Bash pattern denied 5+ times without a permission rule."""
    aggregate: Counter = Counter()
    for s in snap.sessions:
        aggregate.update(s.denied_patterns)
    repeat = [(pat, n) for pat, n in aggregate.most_common() if n >= 5]
    if not repeat:
        return None
    listed = "\n".join(f"  - `{p}` denied {n} time(s)" for p, n in repeat[:5])
    monthly_usd = sum(n for _, n in repeat) * 0.5 * _monthly_factor(snap)
    return Finding(
        id="permission_denies_loop",
        category="hygiene",
        pillar="hygiene",
        title=f"{len(repeat)} repeatedly-denied pattern(s) — candidates for a `permissions.deny` rule",
        evidence=(
            f"The following patterns were denied 5+ times across the window:\n{listed}\n"
            f"Each denial costs an assistant turn plus a user permission prompt — "
            f"and the pattern is not in `permissions.deny`, so Claude keeps trying."
        ),
        savings_usd_per_month=max(monthly_usd, 8.0),
        hours_to_implement=0.1,
        confidence=0.7,
        how_to_fix=(
            "Add the patterns to your `~/.claude/settings.json` `permissions` block "
            "so Claude stops trying:\n\n"
            "```json\n"
            "{\n"
            "  \"permissions\": {\n"
            "    \"deny\": [\n"
            "      \"Bash(rm -rf:*)\",\n"
            "      \"Write(/etc/**)\"\n"
            "    ]\n"
            "  }\n"
            "}\n"
            "```\n\n"
            "The mirror move: patterns you've always approved (10+ times) "
            "belong in `allow` — Anthropic's best-practices doc notes that by "
            "the tenth approval you're not really reviewing.\n"
            "Source: https://code.claude.com/docs/en/best-practices\n"
        ),
    )


def detect_opus_for_compaction(snap: Snapshot) -> Finding | None:
    """scanner#15 #14: /compact summary done by Opus instead of Haiku."""
    total = sum(s.opus_compactions for s in snap.sessions)
    if total < 3:
        return None
    # Each Opus compaction summary = ~50K input tokens at Opus input rate ($15/MTok).
    # Same work on Haiku costs ~$0.80/MTok. Saving = (15 - 0.80) * 50K / 1M per event.
    monthly_usd = total * 50_000 * (15.0 - 0.80) / 1_000_000 * _monthly_factor(snap)
    return Finding(
        id="opus_for_compaction",
        category="workflow",
        pillar="2",
        title=f"{total} /compact event(s) ran on Opus — Haiku would do this work for ~19x less",
        evidence=(
            f"{total} /compact invocation(s) in the window happened while the active "
            f"model was Opus and the input was substantial (>50K tokens). Compaction "
            f"is summarization, not reasoning."
        ),
        savings_usd_per_month=max(monthly_usd, 15.0),
        hours_to_implement=0.1,
        confidence=0.55,
        how_to_fix=(
            "Before running `/compact`, switch to Haiku:\n\n"
            "```\n"
            "/model claude-haiku-4-5\n"
            "/compact\n"
            "```\n\n"
            "Then `/model claude-opus-4-7` (or whatever you were on) for the next "
            "turn. Or define a slash command in `~/.claude/commands/compact-cheap.md` "
            "that runs both steps. The summary quality difference between Haiku and "
            "Opus for summarization is negligible; the cost difference is ~19x.\n"
            "Source: https://code.claude.com/docs/en/costs\n"
        ),
    )


# ============================================================================

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
    # v0.12.6 additions — scanner#15 candidates 1, 2, 3, 4, 5, 6, 8, 9, 13, 14, 15
    detect_mcp_zombie_servers,
    detect_parallel_tools_underused,
    detect_opus_for_subagents,
    detect_subagent_avoidance_on_huge_context,
    detect_tool_search_off_with_many_servers,
    detect_bash_cat_instead_of_read,
    detect_cache_thrash_short_gaps,
    detect_effort_high_for_trivial_work,
    detect_hook_token_burner,
    detect_permission_denies_loop,
    detect_opus_for_compaction,
    # v0.12.8 — scanner#9 fix
    detect_well_routed_subagents,
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
