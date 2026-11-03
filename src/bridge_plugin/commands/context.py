"""7-Day Organizational Context Gate (`bridge context`) and Morning Briefs (`bridge_brief_latest`)."""

from __future__ import annotations

from typing import Any

from ..client import BridgeClient
from ..config import BridgeConfig, get_branch_state, load_local_claims
from ..runtime import (
    detect_git_branch,
    detect_runtime_context,
    emit_output,
)


def _compute_fallback_alignment(data: dict[str, Any]) -> dict[str, Any]:
    """Compute a deterministic 🟢 Green / 🟡 Yellow / 🔴 Red Alignment Check if not provided by server."""
    existing = data.get("alignment")
    if isinstance(existing, dict) and existing.get("status") in ("GREEN", "YELLOW", "RED") and existing.get("badge"):
        return existing

    active_tasks = data.get("activeTasks") if isinstance(data.get("activeTasks"), list) else (
        data.get("tasks") if isinstance(data.get("tasks"), list) else []
    )
    pending_decisions = data.get("pendingDecisions") if isinstance(data.get("pendingDecisions"), list) else [
        d for d in (data.get("decisions") or [])
        if isinstance(d, dict) and d.get("status") in ("OPEN", "IN_REVIEW")
    ]
    resolved_decisions = data.get("resolvedDecisions") if isinstance(data.get("resolvedDecisions"), list) else [
        d for d in (data.get("decisions") or [])
        if isinstance(d, dict) and d.get("status") == "DECIDED"
    ]
    active_leases = data.get("activeLeases") if isinstance(data.get("activeLeases"), list) else (
        data.get("leases") if isinstance(data.get("leases"), list) else []
    )
    corrections = data.get("corrections") if isinstance(data.get("corrections"), list) else []

    blocked_tasks = [
        t for t in active_tasks
        if isinstance(t, dict) and t.get("status") == "BLOCKED_ON_DECISION"
    ]
    critical_decisions = [
        d for d in pending_decisions
        if isinstance(d, dict) and (d.get("urgency") in ("CRITICAL", "HIGH") or d.get("blockedTaskId"))
    ]
    spoken_claim_tasks = [
        t for t in active_tasks
        if isinstance(t, dict) and (
            t.get("railClassification") == "SPOKEN_CLAIM_RAIL_2"
            or t.get("verificationTier") == "TIER2_CANDIDATE"
        )
    ]

    status = "GREEN"
    badge = "🟢 Green"
    reasons: list[str] = []

    if blocked_tasks or critical_decisions:
        status = "RED"
        badge = "🔴 Red"
        if blocked_tasks:
            ids = ", ".join(str(t.get("id", "")) for t in blocked_tasks if t.get("id"))
            reasons.append(f"{len(blocked_tasks)} task(s) blocked on open decision ({ids})")
        if critical_decisions:
            reasons.append(f"{len(critical_decisions)} high-urgency/blocking decision(s) awaiting resolution")
    elif pending_decisions or active_leases or spoken_claim_tasks or corrections:
        status = "YELLOW"
        badge = "🟡 Yellow"

    if active_leases:
        reasons.append(f"{len(active_leases)} active file scope lease(s) held across worktrees")
    if resolved_decisions:
        reasons.append(f"{len(resolved_decisions)} resolved decision(s) in effect — do not violate winning options")
    if spoken_claim_tasks:
        reasons.append(
            f"{len(spoken_claim_tasks)} task(s) have Rail 2 spoken claims only — require Rail 3-5 artifact proof before marking done"
        )
    if corrections:
        reasons.append(f"{len(corrections)} recent 72-hour brief correction(s) active")
    if not reasons:
        reasons.append("No blocking decisions, conflicting scope leases, or uncorroborated claims detected")

    return {
        "status": status,
        "badge": badge,
        "reasons": reasons,
    }


def _format_context_human(payload: dict[str, Any]) -> list[str]:
    """Render a concise human/agent terminal summary of the 7-day organizational context."""
    alignment = payload.get("alignment") or _compute_fallback_alignment(payload)
    badge = alignment.get("badge", "🟢 Green")
    reasons = alignment.get("reasons") or []

    lines: list[str] = [
        f"Bridge Organizational Context ({payload.get('windowDays', 7)}d Window) — Alignment Check: {badge}",
        f"  Organization : {payload.get('organizationId') or '(default)'}",
        f"  Repository   : {payload.get('repository') or '(workspace)'}",
        f"  Branch       : {payload.get('gitBranch') or 'main'}",
    ]
    if payload.get("team"):
        team_obj = payload["team"]
        team_label = team_obj.get("name") or team_obj.get("slug") if isinstance(team_obj, dict) else str(team_obj)
        lines.append(f"  Team Scope   : {team_label}")

    if reasons:
        lines.append("  Alignment    :")
        for reason in reasons:
            lines.append(f"    - {reason}")

    resolved_decisions = payload.get("resolvedDecisions") or []
    pending_decisions = payload.get("pendingDecisions") or []
    if resolved_decisions or pending_decisions:
        lines.append("")
        lines.append(f"Decisions ({len(pending_decisions)} pending, {len(resolved_decisions)} resolved):")
        for dec in resolved_decisions[:5]:
            if not isinstance(dec, dict):
                continue
            win = dec.get("winningOption") or {}
            win_label = win.get("label") if isinstance(win, dict) else None
            crit_names = ", ".join(
                str(c.get("name")) for c in (dec.get("criteria") or []) if isinstance(c, dict) and c.get("name")
            )
            lines.append(
                f"  [DECIDED — DO NOT VIOLATE] {dec.get('id')}: {dec.get('title')} "
                f"-> Winning Option: {win_label or dec.get('chosenOptionId') or '(chosen)'}"
                + (f" (Criteria: {crit_names})" if crit_names else "")
            )
        for dec in pending_decisions[:5]:
            if not isinstance(dec, dict):
                continue
            lines.append(
                f"  [{dec.get('urgency', 'STANDARD')} / {dec.get('status', 'OPEN')}] "
                f"{dec.get('id')}: {dec.get('title')}"
                + (f" (blocks {dec.get('blockedTaskId')})" if dec.get("blockedTaskId") else "")
            )

    tasks = payload.get("tasks") or []
    if tasks:
        lines.append("")
        lines.append(f"Tasks ({len(tasks)} active/recently verified):")
        for task in tasks[:10]:
            if not isinstance(task, dict):
                continue
            rail_label = task.get("railTierLabel") or task.get("verificationTier") or "UNVERIFIED"
            quote = task.get("speakerQuote")
            speaker = task.get("speakerName")
            citations = task.get("citations") or []
            cite_str = ""
            if citations and isinstance(citations[0], dict):
                src = citations[0]
                cite_str = f" [cite: {src.get('sourceProvider', 'source')}:{src.get('sourceUrl') or src.get('sourceObjectId', '')}]"
            lines.append(
                f"  - [{task.get('status', 'TODO')} | {rail_label}] {task.get('id')}: {task.get('title')}{cite_str}"
            )
            if quote:
                prefix = f"{speaker}: " if speaker and not str(quote).startswith(f"{speaker}:") else ""
                lines.append(f"      \"{prefix}{quote}\"")
            parent_rationale = (
                task.get("parentSteeringRationale")
                or (task.get("parentTask") or {}).get("decisionRationale")
                or (task.get("decisionRationale") if (task.get("parentTaskId") or task.get("externalParentRef")) else None)
            )
            if parent_rationale:
                lines.append(f'      ↳ Steering Rationale (from Parent): "{parent_rationale}"')

    leases = payload.get("activeLeases") or payload.get("leases") or []
    if leases:
        lines.append("")
        lines.append(f"Active Scope Leases ({len(leases)}):")
        for lease in leases[:5]:
            if not isinstance(lease, dict):
                continue
            globs = ", ".join(lease.get("claimedGlobs") or [])
            lines.append(
                f"  - {lease.get('agentId', 'agent')} on {lease.get('worktreeBranch', 'main')}: {globs}"
            )

    briefs_obj = payload.get("briefs") if isinstance(payload.get("briefs"), dict) else {}
    org_brief = payload.get("orgBrief") or briefs_obj.get("org") or briefs_obj.get("orgBrief")
    team_brief = payload.get("teamBrief") or briefs_obj.get("team") or briefs_obj.get("teamBrief")
    corrections = payload.get("corrections") or []
    if org_brief or team_brief or corrections:
        lines.append("")
        lines.append("Morning Briefs & 72h Corrections:")
        if isinstance(org_brief, dict):
            headline = (org_brief.get("summaryJson") or {}).get("headline") or org_brief.get("headline") or org_brief.get("kind")
            lines.append(f"  - [ORG Brief] {headline}")
        if isinstance(team_brief, dict):
            headline = (team_brief.get("summaryJson") or {}).get("headline") or team_brief.get("headline") or team_brief.get("kind")
            lines.append(f"  - [TEAM Brief] {headline}")
        for corr in corrections[:5]:
            if not isinstance(corr, dict):
                continue
            lines.append(
                f"  - [Correction ({corr.get('status', 'APPLIED')})] {corr.get('authorName') or 'Member'}: {corr.get('body')}"
            )

    return lines


def handle_context(
    args: Any,
    config: BridgeConfig,
    client: BridgeClient,
    *,
    force_json: bool = False,
    stdout_stream: Any = None,
) -> int:
    """Pull 7-day distilled organizational context from GET /api/v1/fleet/context."""
    raw_days = getattr(args, "days", 7)
    try:
        days = max(1, min(30, int(raw_days if raw_days is not None else 7)))
    except (ValueError, TypeError):
        days = 7

    team = getattr(args, "team", None)
    repo = getattr(args, "repo", None) or config.repository

    params: dict[str, Any] = {"days": days}
    if team:
        params["team"] = str(team).strip()
    if repo:
        params["repository"] = str(repo).strip()
    if config.organization_id:
        params["organizationId"] = config.organization_id

    resp = client.request("GET", "/api/v1/fleet/context", params=params)
    if not resp.ok:
        emit_output(
            resp.data,
            f"Error pulling Bridge organizational context: {resp.error or resp.status_code}",
            force_json=force_json,
            stdout_stream=stdout_stream,
        )
        return resp.exit_code

    runtime = detect_runtime_context(cwd=config.workspace_root, env=config.env)
    branch = detect_git_branch(cwd=config.workspace_root, env=config.env)
    branch_state = get_branch_state(config, branch=branch)
    local_claims = load_local_claims(config)
    spool_entries = client.read_spool()

    data = dict(resp.data)
    data.setdefault("ok", True)
    data.setdefault("organizationId", config.organization_id)
    data.setdefault("windowDays", days)
    if team and "team" not in data:
        data["team"] = team
    data.setdefault("repository", repo)
    data.setdefault("gitBranch", branch)
    data.setdefault("branchState", branch_state)
    data.setdefault("runtime", runtime)
    data.setdefault("localClaims", local_claims)
    data.setdefault("offlineSpoolCount", len(spool_entries))
    data.setdefault("tasks", data.get("activeTasks") or [])
    data.setdefault(
        "activeTasks",
        [t for t in (data.get("tasks") or []) if isinstance(t, dict) and t.get("status") != "DONE"],
    )
    data.setdefault(
        "recentlyVerifiedTasks",
        [
            t for t in (data.get("tasks") or [])
            if isinstance(t, dict)
            and (t.get("verificationTier", "UNVERIFIED") != "UNVERIFIED" or t.get("status") == "DONE")
        ],
    )
    data.setdefault("decisions", [])
    data.setdefault(
        "pendingDecisions",
        [
            d for d in (data.get("decisions") or [])
            if isinstance(d, dict) and d.get("status") in ("OPEN", "IN_REVIEW")
        ],
    )
    data.setdefault(
        "resolvedDecisions",
        [
            d for d in (data.get("decisions") or [])
            if isinstance(d, dict) and d.get("status") == "DECIDED"
        ],
    )
    data.setdefault("leases", data.get("activeLeases") or local_claims)
    data.setdefault("activeLeases", data.get("leases") or [])
    data.setdefault("seams", [])
    data.setdefault("corrections", [])
    data["alignment"] = _compute_fallback_alignment(data)

    human_lines = _format_context_human(data)
    emit_output(data, human_lines, force_json=force_json, stdout_stream=stdout_stream)
    return resp.exit_code


def handle_brief_latest(
    args: Any,
    config: BridgeConfig,
    client: BridgeClient,
    *,
    force_json: bool = False,
    stdout_stream: Any = None,
) -> int:
    """Fetch the latest ORG and TEAM Morning Briefs plus active 72h Corrections from GET /api/v1/briefs."""
    params: dict[str, Any] = {}
    team = getattr(args, "team", None)
    kind = getattr(args, "kind", None)
    if team:
        params["team"] = str(team).strip()
    if kind:
        params["kind"] = str(kind).strip().upper()
    if config.organization_id:
        params["organizationId"] = config.organization_id

    resp = client.request("GET", "/api/v1/briefs", params=params)
    if not resp.ok:
        emit_output(
            resp.data,
            f"Error fetching latest Bridge briefs: {resp.error or resp.status_code}",
            force_json=force_json,
            stdout_stream=stdout_stream,
        )
        return resp.exit_code

    data = dict(resp.data)
    data.setdefault("ok", True)
    data.setdefault("organizationId", config.organization_id)
    data.setdefault("briefs", [])
    data.setdefault("corrections", [])

    org_brief = data.get("orgBrief")
    team_brief = data.get("teamBrief")
    corrections = data.get("corrections") or []

    human_lines = [
        f"Bridge Morning Briefs ({data.get('organizationId') or 'workspace'})",
        f"  ORG Brief        : {((org_brief or {}).get('summaryJson') or {}).get('headline') if isinstance(org_brief, dict) else '(none)'}",
        f"  TEAM Brief       : {((team_brief or {}).get('summaryJson') or {}).get('headline') if isinstance(team_brief, dict) else '(none)'}",
        f"  72h Corrections  : {len(corrections)} active",
    ]
    emit_output(data, human_lines, force_json=force_json, stdout_stream=stdout_stream)
    return resp.exit_code


handle_context_pull = handle_context

