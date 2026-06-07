"""Render Snapshot + Findings to a Markdown report. Anonymized.

SPDX-License-Identifier: Apache-2.0
"""
from __future__ import annotations

from datetime import datetime, timezone
from collections import Counter

from analyzer import Snapshot
from patterns import Finding
from anonymize import scrub_path, scrub_text


def _fmt_money(x: float) -> str:
    if x >= 100:
        return f"${x:,.0f}"
    if x >= 10:
        return f"${x:.2f}"
    return f"${x:.2f}"


def _score_bar(v: int, width: int = 10) -> str:
    """ASCII bar for a 0–100 pillar score."""
    filled = round(max(0, min(100, v)) / 100 * width)
    return "█" * filled + "░" * (width - filled)


def _fmt_tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n/1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n/1_000:.0f}K"
    return str(n)


def _tool_mix_line(snap: Snapshot, top: int = 6) -> str:
    mix = snap.tool_mix
    if not mix:
        return "_no tool calls recorded_"
    total = sum(mix.values())
    bits = []
    for name, n in mix.most_common(top):
        pct = 100 * n / total
        bits.append(f"{name} {pct:.0f}%")
    return ", ".join(bits)


def _model_mix(snap: Snapshot) -> str:
    c: Counter = Counter()
    for s in snap.sessions:
        c.update(s.models_used)
    if not c:
        return "_unknown_"
    total = sum(c.values())
    out = []
    for m, n in c.most_common(4):
        # collapse to family
        key = "Opus" if "opus" in m.lower() else (
            "Sonnet" if "sonnet" in m.lower() else (
                "Haiku" if "haiku" in m.lower() else m
            )
        )
        out.append(f"{key} {100*n/total:.0f}%")
    # collapse duplicates after family grouping
    grouped: Counter = Counter()
    for entry in out:
        name, _, pct = entry.partition(" ")
        grouped[name] += float(pct.rstrip("%"))
    return ", ".join(f"{k} {v:.0f}%" for k, v in grouped.most_common())


_PLAN_LABELS = {
    "api": "API (metered)",
    "pro": "Claude Pro (flat)",
    "max": "Claude Max (flat)",
    "unknown": "unknown",
}


def _is_subscription(plan: str) -> bool:
    return plan in ("pro", "max")


def _fmt_savings(savings_usd_per_month: float, plan: str, monthly_api: float) -> str:
    """Plan-aware savings formatter.

    - api / unknown: '$X/mo'  — actual dollar savings on metered billing
    - pro / max:     '~Y% quota' — % of the user's flat-fee monthly quota
      they reclaim by applying this finding. Quota is denominated in
      API-equivalent dollars; the percentage is plan-agnostic.

    Why % and not absolute dollars on subscriptions: routing Opus→Sonnet
    on a Pro/Max plan does NOT lower the $20/$200 bill. The actual benefit
    is "you can do more Claude Code within the same flat fee before hitting
    rate limits." % of quota is the honest unit.
    """
    if not _is_subscription(plan):
        return f"{_fmt_money(savings_usd_per_month)}/mo"
    if monthly_api <= 0:
        return f"{_fmt_money(savings_usd_per_month)}/mo (API-equivalent)"
    pct = min(95.0, 100.0 * savings_usd_per_month / monthly_api)
    return f"~{pct:.0f}% quota"


def render(snap: Snapshot, findings: list[Finding], billing_plan: str = "unknown",
           score: dict | None = None) -> str:
    cfg = snap.config
    n_sessions = len(snap.sessions)
    avg_tools = 0.0
    all_per_turn: list[int] = []
    for s in snap.sessions:
        all_per_turn.extend(s.tools_per_turn)
    if all_per_turn:
        avg_tools = sum(all_per_turn) / len(all_per_turn)

    total_savings = sum(f.savings_usd_per_month for f in findings)
    total_effort = sum(f.hours_to_implement for f in findings)

    # API-equivalent monthly cost: scales the window cost up to a 30-day rate.
    # Used as the denominator for "% quota" math on Pro/Max plans.
    monthly_api = 0.0
    if snap.window_days > 0:
        monthly_api = snap.total_cost * 30.0 / snap.window_days

    plan = billing_plan if billing_plan in _PLAN_LABELS else "unknown"
    subscription = _is_subscription(plan)

    out: list[str] = []
    out.append("# Tokenmin — your Claude Code improvement plan")
    out.append("")
    out.append(
        f"_Generated {datetime.fromtimestamp(snap.generated_at, tz=timezone.utc).strftime('%Y-%m-%d %H:%M UTC')} "
        f"· {snap.window_days}-day window · "
        f"anonymized — no paths, names, secrets, or message content._"
    )
    out.append("")
    out.append(
        "> Findings are graded against the **RMW Claude Code Workflow Optimizer**. "
        "Each one is tagged with the optimizer pillar it lives in. "
        "Pillar 1 (context + config discipline) is where ~80% of the gains live."
    )
    out.append("")

    if score and score.get("grade"):
        out.append("---")
        out.append("")
        prov = " _(provisional — re-run after a week of use)_" if score.get("provisional") else ""
        out.append(f"## Your Tokenmin Score: {score['grade']} · {score.get('composite', 0)}/100")
        out.append("")
        out.append(f"**{score.get('tier', '')}**{prov}")
        out.append("")
        pls = score.get("pillars", {})
        labels = score.get("pillar_labels", {})
        if pls:
            out.append("| Pillar | Score |")
            out.append("|---|---|")
            for p in ("1", "2", "3", "4"):
                if p in pls:
                    out.append(f"| {labels.get(p, p)} | `{_score_bar(pls[p])}` {pls[p]}/100 |")
            out.append("")
        if score.get("percentile") is not None:
            out.append(f"_Better than {int(score['percentile'])}% of developers who ran Tokenmin._")
            out.append("")
        out.append("_Share your score: `tokenmin share`._")
        out.append("")

    out.append("---")
    out.append("")
    out.append("## TL;DR")
    out.append("")
    if findings:
        top = findings[0]
        savings_unit = _fmt_savings(total_savings, plan, monthly_api)
        top_savings_unit = _fmt_savings(top.savings_usd_per_month, plan, monthly_api)
        if subscription:
            out.append(
                f"- **{len(findings)} improvement(s)** identified, "
                f"est. **{savings_unit}** stretch on your flat-fee plan."
            )
            out.append(
                f"  _You pay flat, so these don't lower your bill — they let you do more Claude work within the same cap._"
            )
        else:
            out.append(
                f"- **{len(findings)} improvement(s)** identified, "
                f"est. **{savings_unit}** in token savings + reclaimed time."
            )
        out.append(f"- **Total effort to implement everything:** ~{total_effort:.1f} hrs.")
        out.append(f"- **Start here:** {top.title} ({top_savings_unit}, {top.hours_to_implement:.1f} hrs).")
    else:
        out.append("- No high-confidence improvements detected. Either you're dialed in, or we need more session history. Re-run after a week of use.")
    out.append("")

    out.append("## Usage snapshot")
    out.append("")
    out.append("| Metric | Value |")
    out.append("|---|---|")
    out.append(f"| Sessions analyzed | {n_sessions} |")
    out.append(f"| Total user turns | {sum(s.user_turns for s in snap.sessions)} |")
    out.append(f"| Total assistant turns | {sum(s.assistant_turns for s in snap.sessions)} |")
    out.append(f"| Avg tools per assistant turn | {avg_tools:.1f} |")
    out.append(f"| Input tokens | {_fmt_tokens(snap.total_input_tokens)} |")
    out.append(f"| Output tokens | {_fmt_tokens(snap.total_output_tokens)} |")
    # Dollar reporting only for API/unknown plans. Subscription users (Pro/Max)
    # pay a flat fee — showing a $7K "cost" when they paid $200 was the bug
    # that made Rick distrust the tool in the first place.
    if not subscription:
        out.append(f"| API-equivalent cost (window) | {_fmt_money(snap.total_cost)} |")
    out.append(f"| Billing plan | {_PLAN_LABELS[plan]} |")
    if subscription:
        out.append(
            f"| _Plan note_ | _You pay flat — savings are shown as quota stretch + tokens saved, "
            f"not dollars. Run `tokenmin show --raw` for API-equivalent numbers._ |"
        )
    elif plan == "unknown":
        out.append(
            f"| _Plan note_ | _Set `tokenmin plan pro\\|max\\|api` so savings get the right units._ |"
        )
    out.append(f"| Models used | {_model_mix(snap)} |")
    out.append(f"| Top tools | {_tool_mix_line(snap)} |")
    out.append("")

    out.append("## Your setup")
    out.append("")
    out.append("| Item | Status |")
    out.append("|---|---|")
    out.append(f"| Global `~/.claude/CLAUDE.md` | {'present' if cfg.has_global_claude_md else 'MISSING'} |")
    out.append(f"| Global `settings.json` | {'present' if cfg.has_global_settings else 'MISSING'} |")
    out.append(f"| Hooks configured | {cfg.global_hook_count} |")
    out.append(f"| Permission rules | {cfg.permission_count} |")
    out.append(f"| Custom agents | {len(cfg.custom_agents)} |")
    out.append(f"| Custom skills | {len(cfg.custom_skills)} |")
    out.append(f"| Slash commands | {len(cfg.custom_commands)} |")
    out.append(f"| MCP servers | {len(cfg.mcp_servers)} |")
    out.append(
        f"| Projects with project-level CLAUDE.md | "
        f"{cfg.projects_with_claude_md} / {cfg.projects_total} |"
    )
    out.append("")

    if findings:
        out.append("## Pillar distribution of your findings")
        out.append("")
        out.append("| Pillar | Findings | Optimizer practice |")
        out.append("|---|---|---|")
        pillar_labels = {
            "1": "Context + config discipline (highest ROI)",
            "2": "Model routing",
            "3": "Parallelism, subagents, MCP",
            "4": "Density of expression",
            "hygiene": "Other hygiene",
        }
        pillar_counts: Counter = Counter(f.pillar for f in findings)
        for pillar in ("1", "2", "3", "4", "hygiene"):
            n = pillar_counts.get(pillar, 0)
            if n:
                out.append(f"| **{pillar}** | {n} | {pillar_labels[pillar]} |")
        out.append("")

        # Same impact thresholds as the engine uses for findings_dicts.low_impact
        # (kept in sync — see tokenmin_engine.LOW_IMPACT_* constants).
        LOW_IMPACT_QUOTA_PCT = 2.0
        LOW_IMPACT_USD = 25.0
        def _is_low(f: Finding) -> bool:
            if subscription and monthly_api > 0:
                return (100.0 * f.savings_usd_per_month / monthly_api) < LOW_IMPACT_QUOTA_PCT
            return f.savings_usd_per_month < LOW_IMPACT_USD
        primary = [f for f in findings if not _is_low(f)]
        low_impact = [f for f in findings if _is_low(f)]
        if not primary and findings:
            primary = [findings[0]]
            low_impact = findings[1:]

        out.append("## Recommendations (ranked)")
        out.append("")
        for i, f in enumerate(primary, start=1):
            pillar_tag = f"Pillar {f.pillar}" if f.pillar in {"1", "2", "3", "4"} else "Hygiene"
            savings_unit = _fmt_savings(f.savings_usd_per_month, plan, monthly_api)
            out.append(
                f"### {i}. {f.title}  "
                f"<sub>·  {savings_unit}  ·  "
                f"{f.hours_to_implement:.1f} hrs  ·  "
                f"conf {int(f.confidence*100)}%  ·  "
                f"[{pillar_tag}]</sub>"
            )
            out.append("")
            out.append(f"**Evidence:** {f.evidence}")
            out.append("")
            out.append("**How to fix:**")
            out.append("")
            out.append(f.how_to_fix.strip())
            out.append("")

        if low_impact:
            out.append("## Minor findings")
            out.append("")
            out.append(
                f"_{len(low_impact)} additional finding(s) below the "
                f"{'2% quota' if subscription else '$25/mo'} threshold. "
                f"Surfaced here for completeness; each is plumbing rather than _the_ next thing to fix._"
            )
            out.append("")
            for f in low_impact:
                savings_unit = _fmt_savings(f.savings_usd_per_month, plan, monthly_api)
                pillar_tag = f"Pillar {f.pillar}" if f.pillar in {"1", "2", "3", "4"} else "Hygiene"
                out.append(
                    f"- **{f.title}** — `{f.id}` · {savings_unit} · "
                    f"{f.hours_to_implement:.1f} hrs · conf {int(f.confidence*100)}% · [{pillar_tag}]"
                )
            out.append("")

    out.append("---")
    out.append("")
    out.append("## Methodology + caveats")
    out.append("")
    out.append(
        "- **Source data:** `~/.claude/projects/*/*.jsonl` (Claude Code session "
        "transcripts) + `~/.claude/settings.json`, `CLAUDE.md`, `agents/`, "
        "`skills/`, `commands/`, and any local MCP config."
    )
    out.append(
        "- **Anonymization:** all paths hashed, emails/IPs/keys scrubbed, "
        "user-home replaced with `<USER>`. The Markdown report contains no "
        "raw transcript content."
    )
    out.append(
        "- **Cost estimates** use rough USD/Mtoken rates and assume conservative "
        "savings. Treat as order-of-magnitude, not invoice-accurate."
    )
    if snap.parse_errors or snap.skipped_files:
        out.append(
            f"- **Parse health:** {snap.parse_errors} session(s) failed to parse, "
            f"{snap.skipped_files} skipped (outside window or empty)."
        )
    out.append("")
    out.append("## Sources & attribution")
    out.append("")
    out.append(
        "Tokenmin applies the **RMW Claude Code Workflow Optimizer** "
        "([github.com/watsonrm/rmwcommerce](https://github.com/watsonrm/rmwcommerce/blob/main/claude-code-optimizer.md)) "
        "to your local usage data. The optimizer's prescriptions come from "
        "Anthropic's official documentation and public talks by Boris Cherny "
        "(creator and head of Claude Code at Anthropic)."
    )
    out.append("")
    out.append("- Claude Code best practices — https://code.claude.com/docs/en/best-practices.md")
    out.append("- CLAUDE.md / memory — https://code.claude.com/docs/en/memory.md")
    out.append("- Quickstart — https://code.claude.com/docs/en/quickstart.md")
    out.append("- Effort parameter — https://platform.claude.com/docs/en/build-with-claude/effort.md")
    out.append("- Anthropic Engineering blog — https://www.anthropic.com/engineering/claude-code-best-practices")
    out.append("")
    return "\n".join(out)
