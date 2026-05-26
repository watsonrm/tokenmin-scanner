# Detector Rules

Rules that apply to every detector in `engine/patterns.py`. Bake new detectors against this list; lift it when audit-bug retrospectives expose a new failure mode.

---

## Rule 1 — Same-surface comparison

A detector that judges *"X is configured but unused"* must compare config and usage data drawn from the **same product surface**.

Tokenmin scans Claude Code session history (`~/.claude/projects/`). Therefore:

| Field | Surface | Safe to compare against Code session data? |
|---|---|---|
| `snap.config.mcp_servers` | Code (`~/.claude.json` + legacy `~/.claude/mcp.json`) | ✅ yes |
| `snap.config.mcp_servers_desktop_only` | Desktop (`claude_desktop_config.json`) | ❌ no — Code never loaded these |
| `snap.config.custom_agents` | Code (`~/.claude/agents/`) | ✅ yes |
| `snap.sessions[*]` | Code transcripts | ✅ yes |
| `snap.sessions[*].tool_calls["mcp__*"]` | Code transcripts | ✅ yes |

If a future field has both Desktop and Code variants, add a `_desktop_only` (or `_<surface>_only`) sibling on `ConfigSnapshot` and route the loader accordingly. Don't merge them.

### Why this matters

The 2026-05-25 incident: `detect_mcp_zombie_servers` flagged `qmd` as a $46/mo zombie. `qmd` was configured in `~/Library/Application Support/Claude/claude_desktop_config.json` only — Code never loaded its schema, so the "schema cost" was zero in the surface tokenmin scanned. The finding was a false positive by construction.

Until this rule was written, the loader read whatever MCP config it found first across both surfaces and merged them silently. Detectors then evaluated Desktop config against Code session evidence — guaranteed surface mismatch.

### What this rule does NOT say

- It does not say a detector can never report on the Desktop surface. It says a single detector must not compare Code config to Desktop usage or vice versa. A future "Desktop zombie" detector that scans Desktop session history is fine — it just needs its own evidence source.
- It does not forbid cross-surface findings entirely. A meta-finding ("you have Desktop config but no Desktop sessions — delete the config?") is reasonable, but it must be explicit about what's compared.

---

## Rule 2 — CLI-as-MCP alternative

A user can wire the same tool into MCP and also call it as a CLI from Bash. Detectors that judge MCP usage must not assume zero MCP calls means the tool is unused — the user may be calling the binary directly (cheaper, no schema injection).

For `detect_mcp_zombie_servers` this still produces the right recommendation (the schema cost is real regardless of whether the CLI is also used), but the `how_to_fix` text must acknowledge the case so users don't think they're being told to delete a tool they actively use.

### What this rule does NOT say

- It does not require detectors to scan Bash invocations from transcripts. They can if they want, but the floor is: don't claim a tool is unused; claim its MCP form is.

---

## Rule 3 — Confidence reflects assumption stack

Each Finding has a `confidence` field. Confidence must drop monotonically as the detector layers in assumptions the snapshot can't directly verify:

- Pure on-disk fact (file exists, count is N) → 0.85+
- On-disk fact + behavioral inference (config present, behavior absent) → 0.60–0.85
- Behavioral inference that crosses surfaces (this needs Rule 1 to pass first) → never above 0.50
- Recommendation that depends on caller intent (e.g., "you probably want X") → 0.20–0.40

A detector that fires high confidence on a behavior-vs-config inference must explicitly note the caveats in its `evidence` string — same shape as `detect_model_overspend`'s "this measures your MAIN session only" caveat after the 2026-05-23 round.

---

## When to add a rule

After a real-world false-positive incident. Don't speculate. The rule should name the incident, the field involved, and the loader/detector pair that produced the bug.
