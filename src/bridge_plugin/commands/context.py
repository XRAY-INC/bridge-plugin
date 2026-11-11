"""7-Day Organizational Context Gate (`bridge context`), Custom Instructions (`bridge instructions`), and Morning Briefs."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..client import BridgeClient
from ..config import (
    BridgeConfig,
    get_branch_state,
    load_cached_custom_instructions,
    load_local_claims,
    save_cached_custom_instructions,
    save_cached_roster,
)
from ..runtime import (
    EXIT_ERROR,
    EXIT_OK,
    detect_git_branch,
    detect_runtime_context,
    emit_output,
    resolve_stdin_or_arg,
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

    open_dec_blocked_ids = {
        str(d.get("blockedTaskId"))
        for d in pending_decisions
        if isinstance(d, dict) and d.get("blockedTaskId")
    }
    blocked_tasks = [
        t for t in active_tasks
        if isinstance(t, dict) and t.get("status") == "BLOCKED_ON_DECISION"
    ]
    unframed_sources = {"AIRTABLE", "ASANA", "MONDAY", "NOTION"}
    decision_blocked_tasks = [
        t for t in blocked_tasks
        if str(t.get("id", "")) in open_dec_blocked_ids
        or str(t.get("externalSource", "NATIVE")).upper() not in unframed_sources
    ]
    unframed_blocked_tasks = [
        t for t in blocked_tasks
        if str(t.get("id", "")) not in open_dec_blocked_ids
        and str(t.get("externalSource", "NATIVE")).upper() in unframed_sources
    ]
    effective_red_blocked = decision_blocked_tasks
    critical_decisions = [
        d for d in pending_decisions
        if isinstance(d, dict)
        and d.get("deliveryMode") != "ADVISORY"
        and (d.get("urgency") in ("CRITICAL", "HIGH") or d.get("blockedTaskId"))
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

    if effective_red_blocked or critical_decisions:
        status = "RED"
        badge = "🔴 Red"
        if effective_red_blocked:
            ids = ", ".join(str(t.get("id", "")) for t in effective_red_blocked if t.get("id"))
            reasons.append(f"{len(effective_red_blocked)} task(s) blocked on open decision ({ids})")
        if critical_decisions:
            reasons.append(f"{len(critical_decisions)} high-urgency/blocking decision(s) awaiting resolution")
    elif pending_decisions or unframed_blocked_tasks or active_leases or spoken_claim_tasks or corrections:
        status = "YELLOW"
        badge = "🟡 Yellow"

    if unframed_blocked_tasks and not effective_red_blocked:
        ids = ", ".join(str(t.get("id", "")) for t in unframed_blocked_tasks[:5] if t.get("id"))
        reasons.append(
            f"{len(unframed_blocked_tasks)} externally-blocked task(s) have no Decision Card framed yet ({ids})"
        )

    if active_leases:
        reasons.append(f"{len(active_leases)} active file scope lease(s) held across worktrees")
    if resolved_decisions:
        reasons.append(f"{len(resolved_decisions)} resolved decision(s) in effect — do not violate winning options")
    if spoken_claim_tasks:
        reasons.append(
            f"{len(spoken_claim_tasks)} task(s) have uncorroborated spoken claims only — require verified artifact proof before marking done"
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
        *(
            [f"  Upstream Org : {payload['agencyContext'].get('organizationId')}"]
            if isinstance(payload.get("agencyContext"), dict) and payload["agencyContext"].get("organizationId")
            else []
        ),
        f"  Repository   : {payload.get('repository') or '(workspace)'}",
        f"  Branch       : {payload.get('gitBranch') or 'main'}",
    ]
    if payload.get("team"):
        team_obj = payload["team"]
        team_label = team_obj.get("name") or team_obj.get("slug") if isinstance(team_obj, dict) else str(team_obj)
        lines.append(f"  Team Scope   : {team_label}")

    roster = payload.get("teamRoster") if isinstance(payload.get("teamRoster"), list) else []
    if roster:
        roster_labels = [
            f"{m.get('handle') or m.get('name')} <{m.get('email')}>"
            for m in roster[:10]
            if isinstance(m, dict) and m.get("email")
        ]
        if roster_labels:
            lines.append(f"  Team Roster  : {', '.join(roster_labels)}")

    if reasons:
        lines.append("  Alignment    :")
        for reason in reasons:
            lines.append(f"    - {reason}")

    cfg_health = payload.get("configurationHealth")
    if isinstance(cfg_health, dict) and "score" in cfg_health:
        score = cfg_health.get("score", 0)
        grade = cfg_health.get("grade", "")
        h_status = cfg_health.get("status", "READY")
        warnings = cfg_health.get("warnings") if isinstance(cfg_health.get("warnings"), list) else []
        lines.append(f"  Readiness    : {score}/100 ({grade} · {h_status})")
        for w in warnings[:3]:
            lines.append(f"    ⚠️ {w}")

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

    custom_inst = payload.get("customInstructions")
    if isinstance(custom_inst, dict) and custom_inst.get("enabled") and str(custom_inst.get("markdown") or "").strip():
        version = custom_inst.get("version", 1)
        policy = custom_inst.get("reloadPolicy", "every_session")
        lines.append("")
        lines.append(f"Organization Custom Instructions (v{version} · reload: {policy}):")
        for inst_line in str(custom_inst.get("markdown") or "").strip().splitlines()[:25]:
            lines.append(f"  {inst_line}")

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

    combined_server_roster: list[Any] = list(data.get("teamRoster") or [])

    if "customInstructions" in data:
        try:
            saved_inst = save_cached_custom_instructions(config.workspace_root, data.get("customInstructions"))
            if saved_inst and saved_inst.suffix == ".md" and saved_inst.is_file():
                data["orgInstructionsPath"] = str(saved_inst)
        except Exception:
            pass
    else:
        cached_inst = load_cached_custom_instructions(config.workspace_root)
        if isinstance(cached_inst, dict) and cached_inst.get("enabled") and cached_inst.get("markdown"):
            data["customInstructions"] = cached_inst
            if config.org_instructions_md_path.is_file():
                data["orgInstructionsPath"] = str(config.org_instructions_md_path)

    # Multi-workspace context hydration when an upstream organization is configured.
    if config.agency_organization_id and not config.is_internal_target:
        try:
            agency_cfg = config.for_agency()
            agency_client = BridgeClient(
                agency_cfg,
                agent_id=runtime["agentId"],
                transport=client._transport,
                env=config.env,
            )
            agency_params: dict[str, Any] = {"days": days, "organizationId": agency_cfg.organization_id}
            if repo:
                agency_params["repository"] = str(repo).strip()
            agency_resp = agency_client.request("GET", "/api/v1/fleet/context", params=agency_params)
            if agency_resp.ok and isinstance(agency_resp.data, dict):
                agency_roster = agency_resp.data.get("teamRoster") or []
                if isinstance(agency_roster, list):
                    combined_server_roster.extend(agency_roster)
                data["agencyOrganizationId"] = agency_cfg.organization_id
                agency_ctx_payload: dict[str, Any] = {
                    "organizationId": agency_cfg.organization_id,
                    "teamRoster": agency_roster,
                    "resolvedDecisions": agency_resp.data.get("resolvedDecisions") or [],
                    "pendingDecisions": agency_resp.data.get("pendingDecisions") or [],
                    "seams": agency_resp.data.get("seams") or [],
                }
                if isinstance(agency_resp.data.get("customInstructions"), dict):
                    agency_ctx_payload["customInstructions"] = agency_resp.data["customInstructions"]
                data["agencyContext"] = agency_ctx_payload
        except Exception:
            pass

    if combined_server_roster:
        try:
            save_cached_roster(config.workspace_root, combined_server_roster)
        except Exception:
            pass

    from .statusbar import format_status_bar_line

    sb_line = format_status_bar_line(config)
    data["statusBar"] = sb_line
    human_lines = _format_context_human(data)
    if sb_line:
        human_lines.append(f"\n{sb_line}")
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


def handle_instructions(
    args: Any,
    config: BridgeConfig,
    client: BridgeClient,
    *,
    force_json: bool = False,
    stdin_stream: Any = None,
    stdout_stream: Any = None,
) -> int:
    """Show, pull, update, or clear organization-wide custom instructions (/api/v1/settings/custom-instructions)."""
    action = str(getattr(args, "instructions_action", None) or "show").strip().lower()
    if getattr(args, "pull", False) and action == "show":
        action = "pull"

    params: dict[str, Any] = {}
    if config.organization_id:
        params["organizationId"] = config.organization_id

    if action == "set":
        file_arg = getattr(args, "file", None)
        markdown_text: str | None = None
        if file_arg:
            fp = Path(str(file_arg)).expanduser()
            if not fp.is_file():
                emit_output(
                    {"ok": False, "error": "FILE_NOT_FOUND", "message": f"File not found: {fp}"},
                    f"Error: File not found: {fp}",
                    force_json=force_json,
                    stdout_stream=stdout_stream,
                )
                return EXIT_ERROR
            markdown_text = fp.read_text(encoding="utf-8")
        else:
            raw_md = getattr(args, "body_flag", None) or getattr(args, "markdown", None)
            markdown_text = resolve_stdin_or_arg(raw_md, stdin_stream=stdin_stream)

        if markdown_text is None:
            emit_output(
                {
                    "ok": False,
                    "error": "MARKDOWN_REQUIRED",
                    "message": "Provide instructions markdown via positional argument, '--file <path>', or '-' (stdin).",
                },
                "Error: Provide instructions markdown, '--file <path>', or '-' for stdin.",
                force_json=force_json,
                stdout_stream=stdout_stream,
            )
            return EXIT_ERROR

        enabled = not bool(getattr(args, "disable", False))
        if getattr(args, "enable", False):
            enabled = True
        reload_policy = str(getattr(args, "reload_policy", None) or "every_session").strip()

        body: dict[str, Any] = {
            "markdown": markdown_text,
            "enabled": enabled,
            "reloadPolicy": reload_policy,
        }
        if config.organization_id:
            body["organizationId"] = config.organization_id

        resp = client.request("PUT", "/api/v1/settings/custom-instructions", json_body=body)
        if not resp.ok:
            emit_output(
                resp.data,
                f"Error saving organization custom instructions: {resp.error or resp.status_code}",
                force_json=force_json,
                stdout_stream=stdout_stream,
            )
            return resp.exit_code

        inst_obj = (
            resp.data.get("customInstructions")
            if isinstance(resp.data.get("customInstructions"), dict)
            else resp.data
        )
        saved_path = save_cached_custom_instructions(config.workspace_root, inst_obj)
        out = {
            **resp.data,
            "ok": True,
            "cachedPath": str(saved_path) if saved_path else None,
        }
        human = [
            f"Saved organization custom instructions (v{inst_obj.get('version', 1)} · reload: {inst_obj.get('reloadPolicy', reload_policy)} · enabled: {inst_obj.get('enabled', enabled)}).",
            *([f"  Cached to : {saved_path}"] if saved_path else []),
        ]
        emit_output(out, human, force_json=force_json, stdout_stream=stdout_stream)
        return EXIT_OK

    if action == "clear":
        resp = client.request("DELETE", "/api/v1/settings/custom-instructions", params=params)
        if not resp.ok:
            emit_output(
                resp.data,
                f"Error clearing organization custom instructions: {resp.error or resp.status_code}",
                force_json=force_json,
                stdout_stream=stdout_stream,
            )
            return resp.exit_code

        save_cached_custom_instructions(
            config.workspace_root,
            {
                "organizationId": config.organization_id,
                "enabled": False,
                "markdown": "",
                "version": 0,
                "reloadPolicy": "every_session",
            },
        )
        out = {**resp.data, "ok": True, "cleared": True}
        emit_output(
            out,
            "Cleared organization custom instructions and removed local .bridge/org_instructions.md cache.",
            force_json=force_json,
            stdout_stream=stdout_stream,
        )
        return EXIT_OK

    # Default: show or pull
    cached_only = bool(getattr(args, "cached", False))
    cached = load_cached_custom_instructions(config.workspace_root)
    if cached_only or (not config.is_authenticated and not config.is_bound):
        inst = cached or {
            "organizationId": config.organization_id,
            "enabled": False,
            "markdown": "",
            "version": 0,
            "reloadPolicy": "every_session",
        }
        payload = {
            "ok": True,
            "source": "cache" if cached else "empty",
            "customInstructions": inst,
            "orgInstructionsPath": str(config.org_instructions_md_path)
            if config.org_instructions_md_path.is_file()
            else None,
        }
        md_val = str(inst.get("markdown") or "").strip()
        human_lines = (
            [
                f"Organization Custom Instructions (cached · v{inst.get('version', 0)} · reload: {inst.get('reloadPolicy', 'every_session')}):",
                "",
                md_val,
            ]
            if inst.get("enabled") and md_val
            else ["No active organization custom instructions cached in this workspace."]
        )
        emit_output(payload, human_lines, force_json=force_json, stdout_stream=stdout_stream)
        return EXIT_OK

    resp = client.request("GET", "/api/v1/settings/custom-instructions", params=params)
    if not resp.ok:
        if cached:
            md_val = str(cached.get("markdown") or "").strip()
            payload = {
                "ok": True,
                "source": "cache_fallback",
                "customInstructions": cached,
                "orgInstructionsPath": str(config.org_instructions_md_path)
                if config.org_instructions_md_path.is_file()
                else None,
            }
            human_lines = (
                [
                    f"Organization Custom Instructions (offline cache · v{cached.get('version', 0)}):",
                    "",
                    md_val,
                ]
                if cached.get("enabled") and md_val
                else ["No active organization custom instructions cached in this workspace."]
            )
            emit_output(payload, human_lines, force_json=force_json, stdout_stream=stdout_stream)
            return EXIT_OK
        emit_output(
            resp.data,
            f"Error fetching organization custom instructions: {resp.error or resp.status_code}",
            force_json=force_json,
            stdout_stream=stdout_stream,
        )
        return resp.exit_code

    inst_obj = (
        resp.data.get("customInstructions")
        if isinstance(resp.data.get("customInstructions"), dict)
        else resp.data
    )
    saved_path = save_cached_custom_instructions(config.workspace_root, inst_obj)
    md_val = str(inst_obj.get("markdown") or "").strip()
    payload = {
        "ok": True,
        "source": "server",
        "organizationId": inst_obj.get("organizationId") or config.organization_id,
        "customInstructions": inst_obj,
        "orgInstructionsPath": str(saved_path)
        if saved_path and saved_path.suffix == ".md" and saved_path.is_file()
        else None,
    }
    human_lines = (
        [
            f"Organization Custom Instructions (v{inst_obj.get('version', 1)} · reload: {inst_obj.get('reloadPolicy', 'every_session')}):",
            *([f"  Cached to : {saved_path}"] if saved_path and saved_path.suffix == ".md" else []),
            "",
            md_val,
        ]
        if inst_obj.get("enabled") and md_val
        else ["No active organization custom instructions configured for this workspace. Configure them in Bridge Settings or via 'brg instructions set'."]
    )
    emit_output(payload, human_lines, force_json=force_json, stdout_stream=stdout_stream)
    return EXIT_OK


