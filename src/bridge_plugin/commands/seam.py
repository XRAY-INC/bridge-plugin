"""Interstitial seam logging (win, decision, friction, handoff), listing, editing, retraction, and spool flush."""

from __future__ import annotations

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

    idempotency_key = compute_idempotency_key(
        org_id=org_id,
        repo=repo,
        branch=branch,
        commit_sha=commit_sha,
        tag=tag,
        summary=summary,
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
    if config.organization_id:
        body["organizationId"] = config.organization_id
    if config.user_email:
        body["userEmail"] = config.user_email
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

    out_payload = {
        **resp.data,
        "tag": tag,
        "summary": summary,
        "repository": repo,
        "gitBranch": branch,
        "taskId": task_id,
        "jobId": job_id,
        "idempotencyKey": idempotency_key,
    }
    if resp.spooled:
        human = f"[SPOOLED] {tag} ({repo}:{branch}): {summary}"
    elif resp.ok:
        seam_id = seam_obj.get("id", "ok") if seam_obj else "ok"
        human = f"[{tag}] ({seam_id}) {repo}:{branch}" + (f" [task:{task_id}]" if task_id else "") + f" — {summary}"
    else:
        human = f"Error logging seam: {resp.error or resp.data.get('message') or 'unknown error'}"

    emit_output(out_payload, human, force_json=force_json, stdout_stream=stdout_stream)
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
    if getattr(args, "limit", None):
        params["limit"] = int(args.limit)

    resp = client.request("GET", "/api/v1/fleet/seam", params=params)
    seams = resp.data.get("seams", []) if isinstance(resp.data.get("seams"), list) else []
    rows = [
        [
            s.get("id", ""),
            s.get("tag", ""),
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

    new_summary = resolve_stdin_or_arg(getattr(args, "summary", None), stdin_stream=stdin_stream)
    if not new_summary:
        emit_output(
            {"ok": False, "error": "SUMMARY_REQUIRED", "message": "New summary is required."},
            "Error: New summary is required.",
            force_json=force_json,
            stdout_stream=stdout_stream,
        )
        return EXIT_ERROR

    body: dict[str, Any] = {
        "id": seam_id,
        "seamId": seam_id,
        "summary": new_summary,
    }
    if getattr(args, "tag", None):
        body["tag"] = str(args.tag).strip().upper()
    if config.organization_id:
        body["organizationId"] = config.organization_id

    resp = client.request("PATCH", "/api/v1/fleet/seam", json_body=body)
    human = (
        f"Updated seam {seam_id}: {new_summary}"
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
