"""Tasks (list, create, start, verify) and Agent Jobs (list, dispatch, claim, complete) commands."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any

from ..client import BridgeClient
from ..config import BridgeConfig, bind_branch_state, get_branch_state
from ..runtime import (
    EXIT_CONCURRENCY_BLOCK,
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


def _slugify(text: str, max_len: int = 36) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9]+", "-", text.strip().lower()).strip("-")
    return (cleaned[:max_len].rstrip("-")) or "work"


def handle_tasks_list(
    args: Any,
    _config: BridgeConfig,
    client: BridgeClient,
    *,
    force_json: bool = False,
    stdout_stream: Any = None,
) -> int:
    """List workspace tasks from /api/v1/tasks."""
    resp = client.request("GET", "/api/v1/tasks")
    tasks = resp.data.get("tasks", []) if isinstance(resp.data.get("tasks"), list) else []
    status_filter = str(getattr(args, "status", "") or "").strip().upper()
    if status_filter:
        tasks = [
            t for t in tasks
            if isinstance(t, dict) and str(t.get("status", "")).upper() == status_filter
        ]
    else:
        tasks = [
            t for t in tasks
            if isinstance(t, dict) and str(t.get("status", "")).upper() != "CANCELED"
        ]

    rows = [
        [
            t.get("id", ""),
            t.get("status", "TODO"),
            t.get("verificationTier", "UNVERIFIED"),
            t.get("externalSource", "NATIVE"),
            (t.get("title") or "")[:52],
        ]
        for t in tasks
        if isinstance(t, dict)
    ]
    payload = {**resp.data, "tasks": tasks}
    table = format_table(["ID", "STATUS", "VERIFICATION", "SOURCE", "TITLE"], rows)
    emit_output(payload, table, force_json=force_json, stdout_stream=stdout_stream)
    return resp.exit_code


def handle_tasks_create(
    args: Any,
    config: BridgeConfig,
    client: BridgeClient,
    *,
    force_json: bool = False,
    stdin_stream: Any = None,
    stdout_stream: Any = None,
) -> int:
    """Create a WorkspaceTask via POST /api/v1/tasks."""
    title = resolve_stdin_or_arg(getattr(args, "title", None), stdin_stream=stdin_stream)
    if not title or len(title.strip()) < 2:
        emit_output(
            {"ok": False, "error": "TITLE_REQUIRED", "message": "Task title (min 2 chars) is required."},
            "Error: Task title is required.",
            force_json=force_json,
            stdout_stream=stdout_stream,
        )
        return EXIT_ERROR

    description = str(getattr(args, "description", "") or "").strip()
    external_source = str(getattr(args, "source", "NATIVE") or "NATIVE").strip().upper()
    external_id = getattr(args, "external_id", None)
    external_url = getattr(args, "external_url", None)
    tier = str(getattr(args, "tier", "UNVERIFIED") or "UNVERIFIED").strip().upper()
    proof = getattr(args, "proof", None)

    branch = detect_git_branch(cwd=config.workspace_root)
    commit_sha = detect_git_commit_sha(cwd=config.workspace_root)
    repo = config.repository or "workspace-repo"
    idempotency_key = compute_idempotency_key(
        org_id=config.organization_id or "",
        repo=repo,
        branch=branch,
        commit_sha=commit_sha,
        tag=f"TASK_CREATE:{external_source}:{external_id or ''}",
        summary=title.strip(),
    )

    body: dict[str, Any] = {
        "title": title.strip()[:240],
        "description": description[:2000],
        "externalSource": external_source,
        "verificationTier": tier,
    }
    if external_id:
        body["externalId"] = str(external_id).strip()[:80]
    if external_url:
        body["externalUrl"] = str(external_url).strip()[:500]
    if proof:
        body["verificationProof"] = str(proof).strip()[:500]
    parent_id = getattr(args, "parent_id", None) or getattr(args, "parent", None)
    if parent_id:
        body["parentTaskId"] = str(parent_id).strip()
        body["externalParentRef"] = str(parent_id).strip()
    if config.organization_id:
        body["organizationId"] = config.organization_id

    resp = client.request(
        "POST",
        "/api/v1/tasks",
        json_body=body,
        idempotency_key=idempotency_key,
        spool_on_offline=True,
    )
    task_obj = resp.data.get("task") if isinstance(resp.data.get("task"), dict) else {}
    task_id = task_obj.get("id") or resp.data.get("id")
    out = {
        **resp.data,
        "taskId": task_id,
        "idempotencyKey": idempotency_key,
    }
    human = (
        f"[SPOOLED] Queued task creation: {title.strip()}"
        if resp.spooled
        else f"Created task {task_id}: {title.strip()}"
        if resp.ok
        else f"Error creating task: {resp.error}"
    )
    emit_output(out, human, force_json=force_json, stdout_stream=stdout_stream)
    return resp.exit_code


def handle_tasks_update(
    args: Any,
    config: BridgeConfig,
    client: BridgeClient,
    *,
    force_json: bool = False,
    stdout_stream: Any = None,
) -> int:
    """Update a WorkspaceTask status, parent link, or rationale via PATCH /api/v1/tasks."""
    task_id = str(getattr(args, "task_id", "") or "").strip()
    if not task_id:
        emit_output(
            {"ok": False, "error": "TASK_ID_REQUIRED", "message": "taskId is required."},
            "Error: taskId is required.",
            force_json=force_json,
            stdout_stream=stdout_stream,
        )
        return EXIT_ERROR

    body: dict[str, Any] = {"taskId": task_id}
    status_val = getattr(args, "status", None)
    if status_val:
        body["status"] = str(status_val).strip().upper()
    parent_id = getattr(args, "parent_id", None) or getattr(args, "parent", None)
    if parent_id is not None:
        body["parentTaskId"] = str(parent_id).strip() or None
        body["externalParentRef"] = str(parent_id).strip() or None
    reason = getattr(args, "reason", None)
    if reason is not None:
        body["aiJudgeReasoning"] = str(reason).strip()[:500]
        body["decisionRationale"] = str(reason).strip()[:500]
    tier = getattr(args, "tier", None)
    if tier:
        body["verificationTier"] = str(tier).strip().upper()
    if config.organization_id:
        body["organizationId"] = config.organization_id

    resp = client.request("PATCH", "/api/v1/tasks", json_body=body)
    task_obj = resp.data.get("task") if isinstance(resp.data.get("task"), dict) else {}
    out = {**resp.data, "taskId": task_obj.get("id") or task_id}
    human = (
        f"Updated task {task_id} (status={task_obj.get('status') or body.get('status', 'unchanged')}, parent={task_obj.get('externalParentRef') or body.get('externalParentRef')})"
        if resp.ok
        else f"Error updating task {task_id}: {resp.error}"
    )
    emit_output(out, human, force_json=force_json, stdout_stream=stdout_stream)
    return resp.exit_code


def handle_tasks_cancel(
    args: Any,
    config: BridgeConfig,
    client: BridgeClient,
    *,
    force_json: bool = False,
    stdout_stream: Any = None,
) -> int:
    """Cancel one or more duplicate/superseded WorkspaceTasks via PATCH /api/v1/tasks."""
    raw_ids = getattr(args, "task_ids", None) or []
    if isinstance(raw_ids, str):
        raw_ids = [raw_ids]
    task_ids = [str(tid).strip() for tid in raw_ids if str(tid).strip()]
    if not task_ids:
        emit_output(
            {"ok": False, "error": "TASK_ID_REQUIRED", "message": "At least one taskId is required."},
            "Error: At least one taskId is required.",
            force_json=force_json,
            stdout_stream=stdout_stream,
        )
        return EXIT_ERROR

    reason = str(getattr(args, "reason", "") or "Canceled via Bridge CLI").strip()[:500]
    canceled: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []

    for tid in task_ids:
        body: dict[str, Any] = {
            "taskId": tid,
            "status": "CANCELED",
            "aiJudgeReasoning": reason,
        }
        if config.organization_id:
            body["organizationId"] = config.organization_id
        resp = client.request("PATCH", "/api/v1/tasks", json_body=body)
        if resp.ok:
            task_obj = resp.data.get("task") if isinstance(resp.data.get("task"), dict) else {}
            canceled.append({
                "taskId": tid,
                "status": task_obj.get("status", "CANCELED"),
                "title": task_obj.get("title", ""),
            })
        else:
            errors.append({"taskId": tid, "error": resp.error})

    ok = len(errors) == 0
    payload = {
        "ok": ok,
        "canceledCount": len(canceled),
        "canceled": canceled,
        "errors": errors,
        "reason": reason,
    }
    human_lines = [
        f"Canceled {tid} ({item.get('title', '')})" for item in canceled for tid in [item.get("taskId")]
    ]
    for err in errors:
        human_lines.append(f"Error canceling {err['taskId']}: {err['error']}")
    emit_output(payload, human_lines or "No tasks canceled.", force_json=force_json, stdout_stream=stdout_stream)
    return EXIT_OK if ok else EXIT_ERROR




def handle_tasks_start(
    args: Any,
    config: BridgeConfig,
    client: BridgeClient,
    *,
    force_json: bool = False,
    stdout_stream: Any = None,
) -> int:
    """Fetch task from Bridge, verify not blocked by Decision, checkout feat/<taskId>-<slug>, and bind .bridge/state.json."""
    task_id = str(getattr(args, "task_id", "")).strip()
    if not task_id:
        emit_output(
            {"ok": False, "error": "TASK_ID_REQUIRED"},
            "Error: taskId is required.",
            force_json=force_json,
            stdout_stream=stdout_stream,
        )
        return EXIT_ERROR

    task_title: str | None = getattr(args, "slug", None)
    task_status = "TODO"
    target_task: dict[str, Any] | None = None
    task_map: dict[str, dict[str, Any]] = {}

    # Attempt to fetch task metadata when authenticated/bound and online
    if config.is_authenticated or config.is_bound:
        resp = client.request("GET", "/api/v1/tasks", require_auth=False)
        if resp.ok and isinstance(resp.data.get("tasks"), list):
            for item in resp.data["tasks"]:
                if isinstance(item, dict):
                    tid = str(item.get("id") or "")
                    ext_id = str(item.get("externalId") or "")
                    if tid:
                        task_map[tid] = item
                    if ext_id:
                        task_map[ext_id] = item
                    if (tid == task_id or ext_id == task_id) and target_task is None:
                        target_task = item
                        task_title = task_title or str(item.get("title") or "")
                        task_status = str(item.get("status") or "TODO").upper()

    if target_task:
        curr_task = target_task
        visited_ancestors: set[str] = set()
        while curr_task:
            parent_ref = curr_task.get("parentTaskId") or curr_task.get("externalParentRef")
            if not parent_ref or str(parent_ref) in visited_ancestors:
                break
            visited_ancestors.add(str(parent_ref))
            parent_task = task_map.get(str(parent_ref))
            if parent_task:
                if str(parent_task.get("status") or "").upper() == "BLOCKED_ON_DECISION":
                    payload = {
                        "ok": False,
                        "blocked": True,
                        "error": "PARENT_TASK_BLOCKED_ON_DECISION",
                        "taskId": task_id,
                        "parentTaskId": parent_task.get("id") or str(parent_ref),
                        "status": "BLOCKED_ON_DECISION",
                        "message": f"Task '{task_id}' cannot be started because ancestor task '{parent_task.get('id') or parent_ref}' is BLOCKED_ON_DECISION. Resolve the open Decision Card before starting.",
                    }
                    emit_output(
                        payload,
                        f"BLOCKED: Ancestor task '{parent_task.get('id') or parent_ref}' has status BLOCKED_ON_DECISION.",
                        force_json=force_json,
                        stdout_stream=stdout_stream,
                    )
                    return EXIT_CONCURRENCY_BLOCK
                curr_task = parent_task
            else:
                break

    if task_status == "BLOCKED_ON_DECISION":
        payload = {
            "ok": False,
            "blocked": True,
            "error": "BLOCKED_ON_DECISION",
            "taskId": task_id,
            "status": task_status,
            "message": f"Task '{task_id}' is currently BLOCKED_ON_DECISION. Resolve the open Decision Card before starting.",
        }
        emit_output(
            payload,
            f"BLOCKED: Task '{task_id}' has status BLOCKED_ON_DECISION.",
            force_json=force_json,
            stdout_stream=stdout_stream,
        )
        return EXIT_CONCURRENCY_BLOCK

    slug = _slugify(getattr(args, "slug", None) or task_title or task_id)
    branch_name = getattr(args, "branch", None) or f"feat/{task_id}-{slug}"
    worktree_path: str | None = None

    git_dir = config.workspace_root / ".git"
    if git_dir.exists() and not getattr(args, "no_checkout", False):
        if getattr(args, "worktree", False):
            wt_dir = config.workspace_root / ".worktrees" / f"{task_id}-{slug}"
            wt_dir.parent.mkdir(parents=True, exist_ok=True)
            subprocess.run(
                ["git", "worktree", "add", "-b", branch_name, str(wt_dir)],
                cwd=str(config.workspace_root),
                capture_output=True,
                text=True,
                check=False,
            )
            worktree_path = str(wt_dir)
        else:
            res = subprocess.run(
                ["git", "checkout", "-b", branch_name],
                cwd=str(config.workspace_root),
                capture_output=True,
                text=True,
                check=False,
            )
            if res.returncode != 0:
                subprocess.run(
                    ["git", "checkout", branch_name],
                    cwd=str(config.workspace_root),
                    capture_output=True,
                    text=True,
                    check=False,
                )

    bound = bind_branch_state(
        config,
        branch_name,
        task_id=task_id,
        job_id=getattr(args, "job", None),
        title=task_title or slug,
        worktree_path=worktree_path,
    )

    payload = {
        "ok": True,
        "taskId": task_id,
        "branch": branch_name,
        "worktreePath": worktree_path,
        "boundState": bound,
        "statePath": str(config.state_path),
    }
    human_lines = [
        f"Started task {task_id} on branch '{branch_name}'",
        f"  State Bound : {config.state_path} ({branch_name} -> {task_id})",
    ]
    if worktree_path:
        human_lines.append(f"  Worktree    : {worktree_path}")

    emit_output(payload, human_lines, force_json=force_json, stdout_stream=stdout_stream)
    return EXIT_OK


def handle_tasks_verify(
    args: Any,
    config: BridgeConfig,
    client: BridgeClient,
    *,
    force_json: bool = False,
    stdin_stream: Any = None,
    stdout_stream: Any = None,
) -> int:
    """Ingest a bounded (<=280 char) verification fact for P16 two-sided task reconciliation."""
    branch = detect_git_branch(cwd=config.workspace_root)
    branch_state = get_branch_state(config, branch=branch)

    raw_task_id = getattr(args, "task_id", None) or branch_state.get("taskId")
    if not raw_task_id:
        emit_output(
            {
                "ok": False,
                "error": "TASK_ID_REQUIRED",
                "message": "Provide a taskId or run on a branch bound via 'bridge tasks start'.",
            },
            "Error: taskId is required.",
            force_json=force_json,
            stdout_stream=stdout_stream,
        )
        return EXIT_ERROR

    raw_statement = resolve_stdin_or_arg(getattr(args, "statement", None), stdin_stream=stdin_stream)
    if not raw_statement:
        emit_output(
            {"ok": False, "error": "STATEMENT_REQUIRED", "message": "Fact statement is required."},
            "Error: Fact statement is required.",
            force_json=force_json,
            stdout_stream=stdout_stream,
        )
        return EXIT_ERROR

    bounded_statement = raw_statement[:280]
    kind = str(getattr(args, "kind", "OUTCOME") or "OUTCOME").strip().upper()
    confidence = int(getattr(args, "confidence", 90) or 90)
    commit_sha = detect_git_commit_sha(cwd=config.workspace_root)
    repo = config.repository or "workspace-repo"

    idempotency_key = compute_idempotency_key(
        org_id=config.organization_id or "",
        repo=repo,
        branch=branch,
        commit_sha=commit_sha,
        tag=f"FACT:{kind}:{raw_task_id}",
        summary=bounded_statement,
    )

    fact_item: dict[str, Any] = {
        "kind": kind,
        "statement": bounded_statement,
        "confidence": confidence,
        "ticketRefs": [str(raw_task_id)],
        "repoRefs": [repo],
        "source": {
            "sourceProvider": "bridge-cli",
            "sourceObjectId": f"verify_{raw_task_id}_{idempotency_key[:12]}",
        },
    }
    body: dict[str, Any] = {
        "facts": [fact_item],
        "idempotencyKey": idempotency_key,
    }
    if config.organization_id:
        body["organizationId"] = config.organization_id

    resp = client.request(
        "POST",
        "/api/v1/ingest/facts",
        json_body=body,
        idempotency_key=idempotency_key,
        spool_on_offline=True,
    )
    out = {
        **resp.data,
        "taskId": raw_task_id,
        "statement": bounded_statement,
        "idempotencyKey": idempotency_key,
    }
    human = (
        f"[SPOOLED] Fact verification queued for {raw_task_id}: {bounded_statement}"
        if resp.spooled
        else f"Verified task {raw_task_id} ({kind}): {bounded_statement}"
        if resp.ok
        else f"Error verifying task {raw_task_id}: {resp.error}"
    )
    emit_output(out, human, force_json=force_json, stdout_stream=stdout_stream)
    return resp.exit_code


def handle_jobs_list(
    args: Any,
    _config: BridgeConfig,
    client: BridgeClient,
    *,
    force_json: bool = False,
    stdout_stream: Any = None,
) -> int:
    """List dispatched agent jobs from /api/v1/fleet/jobs."""
    params: dict[str, Any] = {}
    if getattr(args, "status", None):
        params["status"] = str(args.status).upper()
    if getattr(args, "task", None):
        params["taskId"] = args.task

    resp = client.request("GET", "/api/v1/fleet/jobs", params=params)
    jobs = resp.data.get("jobs", []) if isinstance(resp.data.get("jobs"), list) else []
    rows = [
        [
            j.get("id", ""),
            j.get("status", ""),
            j.get("taskId", ""),
            j.get("agentHarness", ""),
            ",".join(j.get("claimedGlobs", [])) if isinstance(j.get("claimedGlobs"), list) else "-",
        ]
        for j in jobs
        if isinstance(j, dict)
    ]
    table = format_table(["JOB_ID", "STATUS", "TASK_ID", "HARNESS", "CLAIMED_GLOBS"], rows)
    emit_output(resp.data, table, force_json=force_json, stdout_stream=stdout_stream)
    return resp.exit_code


def handle_jobs_dispatch(
    args: Any,
    config: BridgeConfig,
    client: BridgeClient,
    *,
    force_json: bool = False,
    stdout_stream: Any = None,
) -> int:
    """Create a ContextCapsule (if not provided) and dispatch a queued AgentJob via POST /api/v1/fleet/jobs."""
    branch = detect_git_branch(cwd=config.workspace_root, env=config.env)
    branch_state = get_branch_state(config, branch=branch)
    task_id = str(
        getattr(args, "task_flag", None)
        or getattr(args, "task_id", None)
        or branch_state.get("taskId")
        or ""
    ).strip()
    if not task_id:
        emit_output(
            {
                "ok": False,
                "error": "TASK_ID_REQUIRED",
                "message": "Provide a taskId or run on a branch bound via 'bridge tasks start'.",
            },
            "Error: taskId is required.",
            force_json=force_json,
            stdout_stream=stdout_stream,
        )
        return EXIT_ERROR

    capsule_id = str(getattr(args, "capsule", "") or "").strip()
    goal_summary = str(getattr(args, "goal", "") or f"Execute task {task_id}").strip()
    verify_cmd = str(getattr(args, "verify_cmd", None) or "npm test").strip()
    harness = str(getattr(args, "harness", None) or "afk").strip()
    raw_frozen = getattr(args, "frozen_globs", None) or "prisma/migrations/**"
    frozen_globs = [g.strip() for g in str(raw_frozen).split(",") if g.strip()]

    if not capsule_id:
        cap_body: dict[str, Any] = {
            "taskId": task_id,
            "goalSummary": goal_summary,
            "frozenGlobs": frozen_globs,
            "verificationCommand": verify_cmd,
        }
        if getattr(args, "markdown", None):
            cap_body["compiledMarkdown"] = str(args.markdown)
        if config.organization_id:
            cap_body["organizationId"] = config.organization_id

        cap_resp = client.request("POST", "/api/v1/fleet/capsules", json_body=cap_body)
        if not cap_resp.ok:
            emit_output(
                cap_resp.data,
                f"Error creating context capsule for task {task_id}: {cap_resp.error}",
                force_json=force_json,
                stdout_stream=stdout_stream,
            )
            return cap_resp.exit_code
        cap_obj = cap_resp.data.get("capsule") if isinstance(cap_resp.data.get("capsule"), dict) else {}
        capsule_id = str(cap_obj.get("id") or cap_resp.data.get("capsuleId") or "").strip()

    dispatch_body: dict[str, Any] = {
        "action": "dispatch",
        "taskId": task_id,
        "contextCapsuleId": capsule_id,
        "agentHarness": harness,
    }
    if config.organization_id:
        dispatch_body["organizationId"] = config.organization_id

    resp = client.request("POST", "/api/v1/fleet/jobs", json_body=dispatch_body)
    job_id = resp.data.get("jobId") or (
        resp.data.get("job", {}).get("id") if isinstance(resp.data.get("job"), dict) else None
    )
    out = {
        **resp.data,
        "taskId": task_id,
        "contextCapsuleId": capsule_id,
        "jobId": job_id,
        "agentHarness": harness,
    }
    human = (
        f"Dispatched job {job_id} for task {task_id} (capsule: {capsule_id}, harness: {harness})."
        if resp.ok
        else f"Error dispatching job for task {task_id}: {resp.error}"
    )
    emit_output(out, human, force_json=force_json, stdout_stream=stdout_stream)
    return resp.exit_code


def handle_jobs_claim(
    args: Any,
    config: BridgeConfig,
    client: BridgeClient,
    *,
    force_json: bool = False,
    stdout_stream: Any = None,
) -> int:
    """Atomically claim a queued agent job (/api/v1/fleet/jobs action=claim); exits code 2 on 409 conflict."""
    job_id = str(getattr(args, "job_id", "")).strip()
    if not job_id:
        emit_output(
            {"ok": False, "error": "JOB_ID_REQUIRED"},
            "Error: jobId is required.",
            force_json=force_json,
            stdout_stream=stdout_stream,
        )
        return EXIT_ERROR

    runtime = detect_runtime_context(
        cwd=config.workspace_root,
        env=config.env,
        explicit_agent=getattr(args, "agent", None),
    )
    branch = getattr(args, "branch", None) or detect_git_branch(cwd=config.workspace_root, env=config.env)
    raw_globs = getattr(args, "globs", None) or "src/**"
    globs = [g.strip() for g in str(raw_globs).split(",") if g.strip()]

    body: dict[str, Any] = {
        "action": "claim",
        "jobId": job_id,
        "repository": config.repository or runtime["repository"],
        "worktreeBranch": branch,
        "claimedGlobs": globs,
        "agentId": runtime["agentId"],
        "modelName": runtime["modelName"],
    }
    if getattr(args, "ttl", None) is not None:
        body["ttlMinutes"] = int(args.ttl)
    if config.organization_id:
        body["organizationId"] = config.organization_id

    resp = client.request("POST", "/api/v1/fleet/jobs", json_body=body)
    if resp.ok:
        job_obj = resp.data.get("job") if isinstance(resp.data.get("job"), dict) else {}
        task_id = job_obj.get("taskId") or resp.data.get("taskId")
        bind_branch_state(config, branch, task_id=task_id, job_id=job_id)

    human = (
        f"Claimed job {job_id} on branch '{branch}' (globs: {', '.join(globs)})."
        if resp.ok
        else f"CONFLICT / ERROR claiming job {job_id}: {resp.data.get('message') or resp.error}"
    )
    emit_output(resp.data, human, force_json=force_json, stdout_stream=stdout_stream)
    return resp.exit_code


def handle_jobs_complete(
    args: Any,
    config: BridgeConfig,
    client: BridgeClient,
    *,
    force_json: bool = False,
    stdout_stream: Any = None,
) -> int:
    """Complete a claimed agent job (/api/v1/fleet/jobs action=complete)."""
    branch = detect_git_branch(cwd=config.workspace_root, env=config.env)
    branch_state = get_branch_state(config, branch=branch)
    job_id = str(getattr(args, "job_id", None) or branch_state.get("jobId") or "").strip()
    if not job_id:
        emit_output(
            {
                "ok": False,
                "error": "JOB_ID_REQUIRED",
                "message": "Provide a jobId or run on a branch with a bound jobId in .bridge/state.json.",
            },
            "Error: jobId is required.",
            force_json=force_json,
            stdout_stream=stdout_stream,
        )
        return EXIT_ERROR

    runtime = detect_runtime_context(cwd=config.workspace_root)
    files_raw = getattr(args, "files", None) or ""
    files_modified = [f.strip() for f in str(files_raw).split(",") if f.strip()]

    body: dict[str, Any] = {
        "action": "complete",
        "jobId": job_id,
        "agentId": runtime["agentId"],
        "commitSha": getattr(args, "commit", None) or runtime["commitSha"],
        "prUrl": getattr(args, "pr", None),
        "receiptSummary": {
            "filesModified": files_modified,
            "verificationPassed": not bool(getattr(args, "failed", False)),
            "notes": getattr(args, "notes", None) or "Completed via bridge CLI",
        },
    }
    if config.organization_id:
        body["organizationId"] = config.organization_id

    resp = client.request("POST", "/api/v1/fleet/jobs", json_body=body)
    human = (
        f"Completed job {job_id}."
        if resp.ok
        else f"Error completing job {job_id}: {resp.error}"
    )
    emit_output(resp.data, human, force_json=force_json, stdout_stream=stdout_stream)
    return resp.exit_code


def handle_tasks_capsule(
    args: Any,
    config: BridgeConfig,
    client: BridgeClient,
    *,
    force_json: bool = False,
    stdout_stream: Any = None,
) -> int:
    """Compile or fetch a grounded Execution Context Capsule for a task (/api/v1/fleet/capsules)."""
    compile_all = bool(getattr(args, "compile_all", False))
    branch = detect_git_branch(cwd=config.workspace_root, env=config.env)
    branch_state = get_branch_state(config, branch=branch)
    task_id = str(getattr(args, "task_id", None) or branch_state.get("taskId") or "").strip()

    if not compile_all and not task_id:
        emit_output(
            {
                "ok": False,
                "error": "TASK_ID_REQUIRED",
                "message": "Provide a taskId, pass --compile-all, or run on a branch bound via 'brg tasks start'.",
            },
            "Error: taskId is required (or pass --compile-all).",
            force_json=force_json,
            stdout_stream=stdout_stream,
        )
        return EXIT_ERROR

    body: dict[str, Any] = {}
    if compile_all:
        body["compileAllOpen"] = True
    else:
        body["taskId"] = task_id

    raw_frozen = getattr(args, "frozen_globs", None)
    if raw_frozen:
        body["frozenGlobs"] = [g.strip() for g in str(raw_frozen).split(",") if g.strip()]
    verify_cmd = getattr(args, "verify_cmd", None)
    if verify_cmd:
        body["verificationCommand"] = str(verify_cmd).strip()
    if config.organization_id:
        body["organizationId"] = config.organization_id

    resp = client.request("POST", "/api/v1/fleet/capsules", json_body=body)
    if not resp.ok and task_id and not compile_all:
        resp = client.request("GET", "/api/v1/fleet/capsules", params={"taskId": task_id, "compile": "1"})

    if compile_all:
        capsules = resp.data.get("capsules", []) if isinstance(resp.data.get("capsules"), list) else []
        human = (
            f"Compiled {len(capsules)} execution capsule(s) for active workspace tasks."
            if resp.ok
            else f"Error compiling capsules: {resp.error}"
        )
        emit_output(resp.data, human, force_json=force_json, stdout_stream=stdout_stream)
        return resp.exit_code

    cap = resp.data.get("capsule") if isinstance(resp.data.get("capsule"), dict) else {}
    markdown = str(cap.get("compiledMarkdown") or "").strip()
    human = (
        markdown
        if (resp.ok and markdown)
        else f"Compiled capsule for task {task_id} (v{cap.get('version', 1)})."
        if resp.ok
        else f"Error fetching capsule for task {task_id}: {resp.error}"
    )
    emit_output(resp.data, human, force_json=force_json, stdout_stream=stdout_stream)
    return resp.exit_code

