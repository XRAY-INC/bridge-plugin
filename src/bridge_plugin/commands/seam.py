"""Interstitial seam logging (win, decision, friction, handoff), listing, editing, retraction, and spool flush."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import re
from typing import Any

from ..client import BridgeClient
from ..config import (
    BridgeConfig,
    get_branch_state,
    get_last_seam_id,
    record_last_seam_id,
)
from ..runtime import (
    EXIT_ERROR,
    EXIT_OK,
    compute_idempotency_key,
    detect_git_branch,
    detect_git_commit_sha,
    detect_runtime_context,
    emit_output,
    format_table,
    resolve_stdin_or_arg,
)

VALID_SEAM_TAGS = ("WIN", "DECISION", "FRICTION", "HANDOFF")
_DATE_ONLY_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
_MAX_FUTURE_SKEW = timedelta(minutes=5)


def normalize_seam_timestamp(
    raw: Any,
    *,
    now: datetime | None = None,
) -> tuple[str | None, str | None, str | None]:
    """Validate and normalize a YYYY-MM-DD or ISO-8601 timestamp string.

    Returns ``(normalized_iso, error_code, error_message)``.
    Bare ``YYYY-MM-DD`` dates normalize to ``YYYY-MM-DDT12:00:00Z`` (noon UTC)
    so calendar day rendering stays consistent across timezones.
    """
    if raw is None:
        return None, None, None
    text = str(raw).strip()
    if not text:
        return None, "INVALID_SEAM_TIMESTAMP", "Seam timestamp cannot be empty."

    ref_now = now or datetime.now(timezone.utc)
    m = _DATE_ONLY_RE.match(text)
    if m:
        year, month, day = int(m.group(1)), int(m.group(2)), int(m.group(3))
        try:
            d = date(year, month, day)
        except ValueError:
            return (
                None,
                "INVALID_SEAM_TIMESTAMP",
                f"Invalid calendar date '{text}'. Expected YYYY-MM-DD.",
            )
        parsed = datetime(d.year, d.month, d.day, 12, 0, 0, tzinfo=timezone.utc)
        # For bare YYYY-MM-DD, allow today's date in any timezone up to UTC+14
        if d > (ref_now + timedelta(hours=14)).date():
            return (
                None,
                "FUTURE_SEAM_TIMESTAMP",
                f"Timestamp '{text}' is in the future; backlogged seams must occur on or before today.",
            )
        if parsed > ref_now:
            parsed = ref_now.astimezone(timezone.utc)
    else:
        iso_candidate = f"{text[:-1]}+00:00" if text.endswith(("Z", "z")) else text
        try:
            parsed = datetime.fromisoformat(iso_candidate)
        except ValueError:
            return (
                None,
                "INVALID_SEAM_TIMESTAMP",
                f"Invalid timestamp '{text}'. Expected YYYY-MM-DD or ISO-8601.",
            )
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        else:
            parsed = parsed.astimezone(timezone.utc)
        if parsed > ref_now + _MAX_FUTURE_SKEW:
            return (
                None,
                "FUTURE_SEAM_TIMESTAMP",
                f"Timestamp '{text}' is in the future; backlogged seams must occur at or before now.",
            )

    if parsed.year < 2000:
        return (
            None,
            "INVALID_SEAM_TIMESTAMP",
            f"Timestamp '{text}' predates year 2000.",
        )

    normalized = parsed.strftime("%Y-%m-%dT%H:%M:%SZ")
    return normalized, None, None


def handle_seam_log(
    args: Any,
    config: BridgeConfig,
    client: BridgeClient,
    *,
    force_json: bool = False,
    stdin_stream: Any = None,
    stdout_stream: Any = None,
) -> int:
    """Log a WIN, DECISION, FRICTION, or HANDOFF seam with branch task auto-inheritance and offline spooling."""
    tag = str(getattr(args, "tag", "WIN")).strip().upper()
    if tag not in VALID_SEAM_TAGS:
        emit_output(
            {
                "ok": False,
                "error": "INVALID_SEAM_TAG",
                "message": f"Invalid seam tag '{tag}'. Expected one of: {', '.join(VALID_SEAM_TAGS)}.",
            },
            f"Error: Invalid seam tag '{tag}'.",
            force_json=force_json,
            stdout_stream=stdout_stream,
        )
        return EXIT_ERROR

    summary = resolve_stdin_or_arg(getattr(args, "summary", None), stdin_stream=stdin_stream)
    if not summary:
        emit_output(
            {
                "ok": False,
                "error": "SUMMARY_REQUIRED",
                "message": "Seam summary cannot be empty.",
            },
            "Error: Seam summary cannot be empty.",
            force_json=force_json,
            stdout_stream=stdout_stream,
        )
        return EXIT_ERROR

    raw_created_at = getattr(args, "created_at", None)
    normalized_created_at, ts_err, ts_msg = normalize_seam_timestamp(raw_created_at)
    if ts_err:
        emit_output(
            {"ok": False, "error": ts_err, "message": ts_msg or "Invalid timestamp."},
            f"Error: {ts_msg or ts_err}",
            force_json=force_json,
            stdout_stream=stdout_stream,
        )
        return EXIT_ERROR
    historical = bool(getattr(args, "historical", False))

    runtime = detect_runtime_context(
        cwd=config.workspace_root,
        env=config.env,
        explicit_agent=getattr(args, "agent", None),
    )
    branch = (
        getattr(args, "branch", None)
        or detect_git_branch(cwd=config.workspace_root, env=config.env)
        or "main"
    )
    commit_sha = getattr(args, "commit", None) or detect_git_commit_sha(cwd=config.workspace_root, env=config.env)

    # Inherit taskId and jobId from .bridge/state.json when not explicitly passed
    branch_state = get_branch_state(config, branch=branch)
    task_id = getattr(args, "task", None) or branch_state.get("taskId")
    job_id = getattr(args, "job", None) or branch_state.get("jobId")
    repo = getattr(args, "repo", None) or config.repository or runtime["repository"]
    org_id = config.organization_id or ""

    idem_summary = f"{summary}|at:{normalized_created_at}" if normalized_created_at else summary
    idempotency_key = compute_idempotency_key(
        org_id=org_id,
        repo=repo,
        branch=branch,
        commit_sha=commit_sha,
        tag=tag,
        summary=idem_summary,
    )

    body: dict[str, Any] = {
        "tag": tag,
        "summary": summary,
        "repository": repo,
        "gitBranch": branch,
        "commitSha": commit_sha,
        "taskId": task_id,
        "agentId": runtime["agentId"],
        "modelName": runtime["modelName"],
        "hostname": runtime["hostname"],
        "idempotencyKey": idempotency_key,
        "metadata": {
            "idempotencyKey": idempotency_key,
            "jobId": job_id,
            "ci": runtime["ci"],
        },
    }
    if normalized_created_at:
        body["createdAt"] = normalized_created_at
        body["loggedAt"] = normalized_created_at
        body["metadata"]["createdAt"] = normalized_created_at
    if historical:
        body["historical"] = True
        body["metadata"]["historical"] = True
    if config.organization_id:
        body["organizationId"] = config.organization_id
    if config.user_email:
        body["userEmail"] = config.user_email
        body["actorEmail"] = config.user_email
    if config.user_name:
        body["metadata"]["userName"] = config.user_name
    if config.user_alias:
        body["metadata"]["userAlias"] = config.user_alias
    effective_project = getattr(args, "project", None) or config.project_name
    if effective_project:
        body["project"] = effective_project
        body["projectName"] = effective_project
    if config.client_name:
        body["metadata"]["clientName"] = config.client_name
    if getattr(args, "confidence", None) is not None:
        body["confidence"] = int(args.confidence)

    resp = client.request(
        "POST",
        "/api/v1/fleet/seam",
        json_body=body,
        idempotency_key=idempotency_key,
        spool_on_offline=True,
    )

    seam_obj = resp.data.get("seam") if isinstance(resp.data.get("seam"), dict) else None
    if seam_obj and seam_obj.get("id"):
        record_last_seam_id(config, str(seam_obj["id"]))

    # Upstream Workspace Rollup: When logging a WIN or HANDOFF to a bound workspace (`organizationId`),
    # optionally mirror a rollup seam when an upstream organization (`agencyOrganizationId`) is configured.
    mirrored_to_agency = False
    if (
        resp.ok
        and config.agency_organization_id
        and not config.is_internal_target
        and tag in ("WIN", "HANDOFF")
    ):
        try:
            agency_cfg = config.for_agency()
            agency_client = BridgeClient(
                agency_cfg,
                agent_id=runtime["agentId"],
                transport=client._transport,
                env=config.env,
            )
            agency_summary = f"[Workspace: {config.organization_id}] {summary}"
            agency_idem = compute_idempotency_key(
                org_id=agency_cfg.organization_id or "",
                repo=repo,
                branch=branch,
                commit_sha=commit_sha,
                tag=tag,
                summary=agency_summary,
            )
            agency_body = {
                **body,
                "organizationId": agency_cfg.organization_id,
                "summary": agency_summary,
                "idempotencyKey": agency_idem,
            }
            agency_resp = agency_client.request(
                "POST",
                "/api/v1/fleet/seam",
                json_body=agency_body,
                idempotency_key=agency_idem,
                spool_on_offline=False,
            )
            mirrored_to_agency = agency_resp.ok
        except Exception:
            mirrored_to_agency = False

    from .statusbar import format_status_bar_line

    sb_line = format_status_bar_line(config)
    out_payload = {
        **resp.data,
        "tag": tag,
        "summary": summary,
        "repository": repo,
        "gitBranch": branch,
        "taskId": task_id,
        "jobId": job_id,
        "idempotencyKey": idempotency_key,
        "historical": historical,
        **({"createdAt": normalized_created_at} if normalized_created_at else {}),
        "statusBar": sb_line,
    }
    if mirrored_to_agency and config.agency_organization_id:
        out_payload["mirroredToAgencyOrg"] = config.agency_organization_id
    badge_parts = [tag]
    if historical:
        badge_parts.append("historical")
    if normalized_created_at:
        badge_parts.append(normalized_created_at[:10])
    badge_label = " · ".join(badge_parts)
    if resp.spooled:
        human = f"[SPOOLED] {badge_label} ({repo}:{branch}): {summary}"
    elif resp.ok:
        seam_id = seam_obj.get("id", "ok") if seam_obj else "ok"
        human = (
            f"[{badge_label}] ({seam_id}) {repo}:{branch}"
            + (f" [task:{task_id}]" if task_id else "")
            + f" — {summary}"
        )
    elif resp.error == "LOCAL_WORKSPACE_UNBOUND":
        human = (
            f"[LOCAL — NOT SENT TO BRIDGE] '{repo}' is in LOCAL mode (no .bridge.json). "
            "Bridge never logs unbound local sessions."
        )
    else:
        human = f"Error logging seam: {resp.error or resp.data.get('message') or 'unknown error'}"

    human_lines = [human, *([sb_line] if sb_line else [])]
    emit_output(out_payload, human_lines, force_json=force_json, stdout_stream=stdout_stream)
    return resp.exit_code


def handle_seam_list(
    args: Any,
    config: BridgeConfig,
    client: BridgeClient,
    *,
    force_json: bool = False,
    stdout_stream: Any = None,
) -> int:
    """List recent seams from /api/v1/fleet/seam."""
    params: dict[str, Any] = {}
    if getattr(args, "repo", None):
        params["repository"] = args.repo
    elif config.repository:
        params["repository"] = config.repository
    if getattr(args, "tag", None):
        params["tag"] = str(args.tag).upper()
    if getattr(args, "task", None):
        params["taskId"] = args.task
    if getattr(args, "project", None):
        params["project"] = args.project
    if getattr(args, "historical_filter", None) in ("true", "false"):
        params["historical"] = args.historical_filter
    if getattr(args, "limit", None):
        params["limit"] = int(args.limit)

    resp = client.request("GET", "/api/v1/fleet/seam", params=params)
    seams = resp.data.get("seams", []) if isinstance(resp.data.get("seams"), list) else []
    rows = [
        [
            s.get("id", ""),
            f"{s.get('tag', '')}{' (hist)' if s.get('historical') else ''}",
            s.get("repository", ""),
            s.get("gitBranch", ""),
            s.get("taskId") or "-",
            (s.get("summary") or "")[:60],
        ]
        for s in seams
        if isinstance(s, dict)
    ]
    table = format_table(["ID", "TAG", "REPO", "BRANCH", "TASK", "SUMMARY"], rows)
    emit_output(resp.data, table, force_json=force_json, stdout_stream=stdout_stream)
    return resp.exit_code


def handle_seam_edit(
    args: Any,
    config: BridgeConfig,
    client: BridgeClient,
    *,
    force_json: bool = False,
    stdin_stream: Any = None,
    stdout_stream: Any = None,
) -> int:
    """Edit an existing seam in place (recording prior revision history)."""
    raw_id = str(getattr(args, "seam_id", "")).strip()
    seam_id = get_last_seam_id(config) if raw_id.lower() == "last" else raw_id
    if not seam_id:
        emit_output(
            {
                "ok": False,
                "error": "SEAM_ID_REQUIRED",
                "message": "Provide a seam ID or 'last' after logging a seam in this workspace.",
            },
            "Error: Seam ID required (no 'last' seam recorded in .bridge/state.json).",
            force_json=force_json,
            stdout_stream=stdout_stream,
        )
        return EXIT_ERROR

    raw_summary = getattr(args, "summary", None)
    new_summary = (
        resolve_stdin_or_arg(raw_summary, stdin_stream=stdin_stream)
        if raw_summary is not None
        else ""
    )
    raw_created_at = getattr(args, "created_at", None)
    normalized_created_at, ts_err, ts_msg = normalize_seam_timestamp(raw_created_at)
    if ts_err:
        emit_output(
            {"ok": False, "error": ts_err, "message": ts_msg or "Invalid timestamp."},
            f"Error: {ts_msg or ts_err}",
            force_json=force_json,
            stdout_stream=stdout_stream,
        )
        return EXIT_ERROR

    historical_flag = getattr(args, "historical", None)
    has_tag_update = bool(getattr(args, "tag", None))
    raw_project = getattr(args, "project", None)
    has_project_update = raw_project is not None
    raw_task = getattr(args, "task", None)
    has_task_update = raw_task is not None
    raw_commit = getattr(args, "commit", None)
    has_commit_update = raw_commit is not None
    bind_repo = bool(getattr(args, "bind_repo", False))

    if (
        not new_summary
        and not normalized_created_at
        and historical_flag is None
        and not has_tag_update
        and not has_project_update
        and not has_task_update
        and not has_commit_update
    ):
        emit_output(
            {
                "ok": False,
                "error": "SUMMARY_REQUIRED",
                "message": "Provide an updated summary, --project, --task, --commit, --tag, --at, or --historical/--live.",
            },
            "Error: Provide an updated summary, --project, --task, --commit, --tag, --at, or --historical/--live.",
            force_json=force_json,
            stdout_stream=stdout_stream,
        )
        return EXIT_ERROR

    body: dict[str, Any] = {
        "id": seam_id,
        "seamId": seam_id,
    }
    if new_summary:
        body["summary"] = new_summary
    if has_tag_update:
        body["tag"] = str(args.tag).strip().upper()
    if has_project_update:
        clean_proj = str(raw_project).strip()
        body["project"] = clean_proj if clean_proj else None
        body["projectName"] = clean_proj if clean_proj else None
    if bind_repo:
        body["bindRepositoryToProject"] = True
    if has_task_update:
        clean_task = str(raw_task).strip()
        body["taskId"] = clean_task if clean_task else None
    if has_commit_update:
        clean_commit = str(raw_commit).strip()
        body["commitSha"] = clean_commit if clean_commit else None
    if normalized_created_at:
        body["createdAt"] = normalized_created_at
    if isinstance(historical_flag, bool):
        body["historical"] = historical_flag
    if config.organization_id:
        body["organizationId"] = config.organization_id

    resp = client.request("PATCH", "/api/v1/fleet/seam", json_body=body)
    human = (
        f"Updated seam {seam_id}" + (f": {new_summary}" if new_summary else ".")
        if resp.ok
        else f"Error updating seam {seam_id}: {resp.error}"
    )
    emit_output(resp.data, human, force_json=force_json, stdout_stream=stdout_stream)
    return resp.exit_code


def handle_seam_retract(
    args: Any,
    config: BridgeConfig,
    client: BridgeClient,
    *,
    force_json: bool = False,
    stdout_stream: Any = None,
) -> int:
    """Soft-retract a seam by ID or 'last'."""
    raw_id = str(getattr(args, "seam_id", "")).strip()
    seam_id = get_last_seam_id(config) if raw_id.lower() == "last" else raw_id
    if not seam_id:
        emit_output(
            {
                "ok": False,
                "error": "SEAM_ID_REQUIRED",
                "message": "Provide a seam ID or 'last'.",
            },
            "Error: Seam ID required.",
            force_json=force_json,
            stdout_stream=stdout_stream,
        )
        return EXIT_ERROR

    body: dict[str, Any] = {
        "id": seam_id,
        "seamId": seam_id,
    }
    if config.organization_id:
        body["organizationId"] = config.organization_id

    resp = client.request("DELETE", "/api/v1/fleet/seam", json_body=body)
    human = (
        f"Retracted seam {seam_id}."
        if resp.ok
        else f"Error retracting seam {seam_id}: {resp.error}"
    )
    emit_output(resp.data, human, force_json=force_json, stdout_stream=stdout_stream)
    return resp.exit_code


def handle_seam_flush(
    _args: Any,
    _config: BridgeConfig,
    client: BridgeClient,
    *,
    force_json: bool = False,
    stdout_stream: Any = None,
) -> int:
    """Flush offline queued mutations from .bridge/spool.jsonl."""
    resp = client.flush_spool()
    flushed = resp.data.get("flushedCount", 0)
    remaining = resp.data.get("remainingCount", 0)
    human = f"Flushed {flushed} queued item(s) from offline spool ({remaining} remaining)."
    emit_output(resp.data, human, force_json=force_json, stdout_stream=stdout_stream)
    return resp.exit_code
