"""Tokenmin engine — adapter implementing the open client's engine contract.

PROPRIETARY. Not part of the open client (Apache-2.0). See engine/LICENSE.

The open client (tokenmin.py) hands the engine an anonymized snapshot as a plain
dict and expects Markdown back:

    tokenmin_engine.analyze(snapshot: dict) -> str

This module reconstructs the open client's `Snapshot` dataclasses from that dict
(the dict was produced by tokenmin.py's `_dataclass_to_dict`: Counters became dicts,
sets became sorted lists), runs the detection rule base, and renders the report.

It depends on the open client's `analyzer` module for the schema definitions —
that is the intended architecture: the proprietary engine consumes the open
client's `Snapshot` contract; it does not redefine it.
"""
from __future__ import annotations

from collections import Counter

from analyzer import Snapshot, SessionStats, ConfigSnapshot
from patterns import run_all
from report import render
from scoring import compute_score


def _monthly_api(snap: Snapshot) -> float:
    """Scale the window's API-equivalent cost up to a 30-day rate."""
    if snap.window_days > 0:
        return snap.total_cost * 30.0 / snap.window_days
    return 0.0


def _session_from_dict(d: dict) -> SessionStats:
    s = SessionStats(
        session_id=d.get("session_id", ""),
        project=d.get("project", ""),
    )
    s.started_at = d.get("started_at")
    s.ended_at = d.get("ended_at")
    s.user_turns = d.get("user_turns", 0)
    s.assistant_turns = d.get("assistant_turns", 0)
    s.tool_calls = Counter(d.get("tool_calls") or {})
    s.tools_per_turn = list(d.get("tools_per_turn") or [])
    s.files_read = Counter(d.get("files_read") or {})
    s.files_written = set(d.get("files_written") or [])
    s.permission_denies = d.get("permission_denies", 0)
    s.error_results = d.get("error_results", 0)
    s.long_searches = d.get("long_searches", 0)
    s.agents_used = Counter(d.get("agents_used") or {})
    s.models_used = Counter(d.get("models_used") or {})
    s.input_tokens = d.get("input_tokens", 0)
    s.output_tokens = d.get("output_tokens", 0)
    s.cache_write_tokens = d.get("cache_write_tokens", 0)
    s.cache_read_tokens = d.get("cache_read_tokens", 0)
    s.est_cost_usd = d.get("est_cost_usd", 0.0)
    s.redo_signals = d.get("redo_signals", 0)
    # v0.12.6 — new per-session fields. Default 0 / empty so older snapshots
    # serialized by pre-0.12.6 clients still round-trip cleanly.
    s.bash_file_ops = d.get("bash_file_ops", 0)
    s.cache_thrash_events = d.get("cache_thrash_events", 0)
    s.thinking_bloat_turns = d.get("thinking_bloat_turns", 0)
    s.hook_event_chars = d.get("hook_event_chars", 0)
    s.hook_event_fires = d.get("hook_event_fires", 0)
    s.denied_patterns = Counter(d.get("denied_patterns") or {})
    s.compacts = d.get("compacts", 0)
    s.compact_then_died = d.get("compact_then_died", 0)
    s.opus_compactions = d.get("opus_compactions", 0)
    s.rate_limit_errors = d.get("rate_limit_errors", 0)  # v0.12.9
    return s


def _config_from_dict(d: dict) -> ConfigSnapshot:
    d = d or {}
    return ConfigSnapshot(
        has_global_settings=d.get("has_global_settings", False),
        has_global_claude_md=d.get("has_global_claude_md", False),
        global_claude_md_lines=d.get("global_claude_md_lines", 0),
        obsolete_references=list(d.get("obsolete_references") or []),
        global_hook_count=d.get("global_hook_count", 0),
        permission_count=d.get("permission_count", 0),
        custom_agents=list(d.get("custom_agents") or []),
        custom_skills=list(d.get("custom_skills") or []),
        custom_commands=list(d.get("custom_commands") or []),
        mcp_servers=list(d.get("mcp_servers") or []),
        mcp_servers_desktop_only=list(d.get("mcp_servers_desktop_only") or []),
        mcp_server_invocations=dict(d.get("mcp_server_invocations") or {}),
        projects_with_claude_md=d.get("projects_with_claude_md", 0),
        projects_with_oversized_claude_md=d.get("projects_with_oversized_claude_md", 0),
        projects_total=d.get("projects_total", 0),
        projects_with_local_settings=d.get("projects_with_local_settings", 0),
        projects_with_local_agents=d.get("projects_with_local_agents", 0),
        # v0.12.4: project-cwd-resolved fields. Default to 0 so older clients
        # sending snapshots without these still work.
        project_cwd_total=d.get("project_cwd_total", 0),
        project_cwd_with_claude_md=d.get("project_cwd_with_claude_md", 0),
        project_cwd_with_local_agents=d.get("project_cwd_with_local_agents", 0),
        project_cwd_with_agent_count=d.get("project_cwd_with_agent_count", 0),
        output_style=d.get("output_style"),
        enable_tool_search=d.get("enable_tool_search"),
    )


def _snapshot_from_dict(d: dict) -> Snapshot:
    return Snapshot(
        generated_at=d.get("generated_at", 0.0),
        window_days=d.get("window_days", 30),
        sessions=[_session_from_dict(s) for s in (d.get("sessions") or [])],
        config=_config_from_dict(d.get("config") or {}),
        parse_errors=d.get("parse_errors", 0),
        skipped_files=d.get("skipped_files", 0),
    )


def analyze(snapshot: dict) -> str:
    """Engine entry point. Anonymized snapshot dict -> Markdown report."""
    snap = _snapshot_from_dict(snapshot)
    findings = run_all(snap)
    score = compute_score(snap, findings, _monthly_api(snap))
    return render(snap, findings, score=score)


def analyze_structured(snapshot: dict, billing_plan: str = "unknown") -> dict:
    """Engine entry point (rich-terminal mode). Returns structured findings
    alongside the markdown report so the client can render an inline,
    color/bar-based card without re-parsing markdown.

    `billing_plan` controls how savings are framed in the markdown report:
      - "api" / "unknown": dollar savings ($X/mo)
      - "pro" / "max":     percent quota stretch (~Y% quota)
    The findings list keeps absolute dollar values in `savings_usd_per_month`
    regardless — the framing is purely presentational.

    Schema:
      {
        "snapshot": {
          "sessions": int,
          "user_turns": int,
          "assistant_turns": int,
          "input_tokens": int,
          "output_tokens": int,
          "total_cost_usd": float,
          "window_days": int,
          "models": [{"name": "Opus", "share": 0.87}, ...],
          "top_tools": [{"name": "Bash", "share": 0.40}, ...],
          "config": {
            "has_global_claude_md": bool,
            "global_claude_md_lines": int,
            "global_hook_count": int,
            "custom_agents": int,
            "custom_skills": int,
            "mcp_servers": int,
            "projects_total": int,
            "projects_with_claude_md": int,
          },
        },
        "findings": [
          {
            "id": "model_overspend",
            "title": "...",
            "evidence": "...",
            "savings_usd_per_month": 1140.0,
            "hours_to_implement": 0.1,
            "confidence": 0.55,
            "category": "config",
            "pillar": "2",
            "how_to_fix": "...",
            "score": <internal score>,
          },
          ...
        ],
        "total_savings_usd_per_month": float,
        "total_hours_to_implement": float,
        "report_md": "<full markdown>",
        "engine_version": "0.4",
      }
    """
    from collections import Counter as _Counter
    snap = _snapshot_from_dict(snapshot)
    findings = run_all(snap)

    # Aggregate model + tool mix.
    model_counter: _Counter = _Counter()
    for s in snap.sessions:
        model_counter.update(s.models_used)
    total_model = sum(model_counter.values()) or 1
    # Group by family (Opus/Sonnet/Haiku/other).
    family: _Counter = _Counter()
    for m, n in model_counter.items():
        ml = m.lower()
        if "opus" in ml: family["Opus"] += n
        elif "sonnet" in ml: family["Sonnet"] += n
        elif "haiku" in ml: family["Haiku"] += n
        else: family["Other"] += n
    # Both count + share so consumers can pick what they need. count is the
    # raw integer; share is count / total (0.0–1.0) for display convenience.
    models_list = [{"name": k, "count": v, "share": v / total_model} for k, v in family.most_common()]

    tool_counter: _Counter = _Counter()
    for s in snap.sessions:
        tool_counter.update(s.tool_calls)
    total_tools = sum(tool_counter.values()) or 1
    top_tools_list = [
        {"name": k, "share": v / total_tools}
        for k, v in tool_counter.most_common(6)
    ]

    findings_dicts = []
    for f in findings:
        findings_dicts.append({
            "id": f.id,
            "title": f.title,
            "evidence": f.evidence,
            "savings_usd_per_month": f.savings_usd_per_month,
            "hours_to_implement": f.hours_to_implement,
            "confidence": f.confidence,
            "category": f.category,
            "pillar": f.pillar,
            "how_to_fix": f.how_to_fix,
            "score": f.score(),
        })

    cfg = snap.config
    # Aggregates needed by telemetry's discovery layer (cache hit ratio,
    # parallelism, per-turn pressure). Computed once here so the client doesn't
    # have to re-walk the snapshot.
    cache_read_total = sum(s.cache_read_tokens for s in snap.sessions)
    cache_write_total = sum(s.cache_write_tokens for s in snap.sessions)
    all_tpt = [n for s in snap.sessions for n in s.tools_per_turn]
    avg_tools_per_turn = sum(all_tpt) / len(all_tpt) if all_tpt else 0.0

    # API-equivalent monthly cost — denominator for plan-aware framing in the
    # CLI's terminal renderer. Same formula the report uses internally.
    monthly_api_cost = 0.0
    if snap.window_days > 0:
        monthly_api_cost = snap.total_cost * 30.0 / snap.window_days

    plan = billing_plan if billing_plan in ("api", "pro", "max", "unknown") else "unknown"

    # v0.12.4 (scanner#4): tag findings as low-impact so the CLI can hide them
    # from the main audit but still surface via `tokenmin show low-impact`.
    # Thresholds: subscription plans use quota %; api/unknown use $/mo.
    LOW_IMPACT_QUOTA_PCT = 2.0   # < 2% of monthly cap is noise on Pro/Max
    LOW_IMPACT_USD = 25.0        # < $25/mo on metered API
    for f in findings_dicts:
        sv = f.get("savings_usd_per_month", 0.0)
        if plan in ("pro", "max") and monthly_api_cost > 0:
            quota_pct = 100.0 * sv / monthly_api_cost
            f["low_impact"] = quota_pct < LOW_IMPACT_QUOTA_PCT
        else:
            f["low_impact"] = sv < LOW_IMPACT_USD

    # Tokenmin Score — composite grade computed from the same findings. Single
    # source of truth (scoring.py) so the terminal card, markdown report, and
    # shareable scorecard never disagree.
    score = compute_score(snap, findings, monthly_api_cost)

    return {
        "snapshot": {
            "sessions": len(snap.sessions),
            "user_turns": sum(s.user_turns for s in snap.sessions),
            "assistant_turns": sum(s.assistant_turns for s in snap.sessions),
            "input_tokens": snap.total_input_tokens,
            "output_tokens": snap.total_output_tokens,
            "cache_read_tokens": cache_read_total,
            "cache_write_tokens": cache_write_total,
            "avg_tools_per_turn": avg_tools_per_turn,
            "total_cost_usd": snap.total_cost,
            "monthly_api_equivalent_cost_usd": monthly_api_cost,
            # v0.12.9 — aggregate signal for the billing-plan heuristic.
            "total_rate_limit_errors": sum(s.rate_limit_errors for s in snap.sessions),
            "window_days": snap.window_days,
            "models": models_list,
            "top_tools": top_tools_list,
            "parse_errors": snap.parse_errors,
            "skipped_files": snap.skipped_files,
            "config": {
                "has_global_claude_md": cfg.has_global_claude_md,
                "global_claude_md_lines": cfg.global_claude_md_lines,
                "global_hook_count": cfg.global_hook_count,
                "custom_agents": len(cfg.custom_agents),
                "custom_skills": len(cfg.custom_skills),
                "mcp_servers": len(cfg.mcp_servers),
                "projects_total": cfg.projects_total,
                "projects_with_claude_md": cfg.projects_with_claude_md,
                "output_style": cfg.output_style,
                "enable_tool_search": cfg.enable_tool_search,
            },
        },
        "findings": findings_dicts,
        "total_savings_usd_per_month": sum(f.savings_usd_per_month for f in findings),
        "total_hours_to_implement": sum(f.hours_to_implement for f in findings),
        "billing_plan": plan,
        "tokenmin_score": score,
        "report_md": render(snap, findings, billing_plan=plan, score=score),
        "engine_version": "0.6",
    }
