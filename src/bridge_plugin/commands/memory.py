"""Working-memory Scratchpad (read/write), Company Memory (list/pin), and Turn Handoff commands."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..client import BridgeClient
from ..config import BridgeConfig, get_branch_state
from ..runtime import (
    EXIT_ERROR,
    compute_idempotency_key,
    detect_git_branch,
    detect_git_commit_sha,
    detect_runtime_context,
    emit_output,
    format_table,
    resolve_stdin_or_arg,
)


def handle_scratchpad_read(
    args: Any,
    config: BridgeConfig,
    client: BridgeClient,
    *,
    force_json: bool = False,
    stdout_stream: Any = None,
) -> int:
    """Read working-memory scratchpad notes from /api/v1/fleet/scratchpad."""
    params: dict[str, Any] = {}
    repo = getattr(args, "repo", None) or config.repository
    if repo:
        params["repository"] = repo
    if getattr(args, "topic", None):
        params["topic"] = args.topic
    if getattr(args, "limit", None):
        params["limit"] = int(args.limit)

    resp = client.request("GET", "/api/v1/fleet/scratchpad", params=params)
    notes = resp.data.get("notes", []) if isinstance(resp.data.get("notes"), list) else []
    rows = [
        [
            n.get("topic", ""),
            n.get("repository", ""),
            n.get("gitBranch", ""),
            n.get("agentId") or str(n.get("updatedByEmail") or "").removesuffix("@workspace.os"),
            (n.get("bodyMarkdown") or n.get("body") or "")[:60],
        ]
        for n in notes
        if isinstance(n, dict)
    ]
    table = format_table(["TOPIC", "REPO", "BRANCH", "AGENT", "BODY"], rows)
    emit_output(resp.data, table, force_json=force_json, stdout_stream=stdout_stream)
    return resp.exit_code


def handle_scratchpad_write(
    args: Any,
    config: BridgeConfig,
    client: BridgeClient,
    *,
    force_json: bool = False,
    stdin_stream: Any = None,
    stdout_stream: Any = None,
) -> int:
    """Upsert a working-memory scratchpad note in /api/v1/fleet/scratchpad with offline spooling."""
    topic = str(getattr(args, "topic", "") or "").strip()
    file_arg = getattr(args, "file", None)
    if file_arg:
        body_text = Path(file_arg).read_text(encoding="utf-8").strip()
    else:
        body_text = resolve_stdin_or_arg(getattr(args, "body", None), stdin_stream=stdin_stream)
    if not topic or not body_text:
        emit_output(
            {
                "ok": False,
                "error": "TOPIC_AND_BODY_REQUIRED",
                "message": "Both topic and markdown body are required.",
            },
            "Error: Provide both <topic> and <body>.",
            force_json=force_json,
            stdout_stream=stdout_stream,
        )
        return EXIT_ERROR

    runtime = detect_runtime_context(cwd=config.workspace_root, env=config.env)
    branch = getattr(args, "branch", None) or detect_git_branch(cwd=config.workspace_root, env=config.env)
    commit_sha = detect_git_commit_sha(cwd=config.workspace_root, env=config.env)
    repo = getattr(args, "repo", None) or config.repository or runtime["repository"]

    idempotency_key = compute_idempotency_key(
        org_id=config.organization_id or "",
        repo=repo,
        branch=branch,
        commit_sha=commit_sha,
        tag=f"SCRATCHPAD:{topic}",
        summary=body_text,
    )

    payload_body: dict[str, Any] = {
        "topic": topic,
        "body": body_text,
        "bodyMarkdown": body_text,
        "repository": repo,
        "gitBranch": branch,
        "agentId": runtime["agentId"],
        "idempotencyKey": idempotency_key,
    }
    if config.organization_id:
        payload_body["organizationId"] = config.organization_id
    if config.user_email:
        payload_body["updatedByEmail"] = config.user_email

    resp = client.request(
        "POST",
        "/api/v1/fleet/scratchpad",
        json_body=payload_body,
        idempotency_key=idempotency_key,
        spool_on_offline=True,
    )
    out = {**resp.data, "topic": topic, "repository": repo, "idempotencyKey": idempotency_key}
    human = (
        f"[SPOOLED] Scratchpad note '{topic}' queued for {repo}:{branch}."
        if resp.spooled
        else f"Saved scratchpad note '{topic}' on {repo}:{branch}."
        if resp.ok
        else f"Error writing scratchpad note: {resp.error}"
    )
    emit_output(out, human, force_json=force_json, stdout_stream=stdout_stream)
    return resp.exit_code


def handle_handoff(
    args: Any,
    config: BridgeConfig,
    client: BridgeClient,
    *,
    force_json: bool = False,
    stdin_stream: Any = None,
    stdout_stream: Any = None,
) -> int:
    """Record a turn-closure HANDOFF seam and update the working-memory scratchpad."""
    summary = resolve_stdin_or_arg(getattr(args, "summary", None), stdin_stream=stdin_stream)
    if not summary:
        emit_output(
            {"ok": False, "error": "SUMMARY_REQUIRED", "message": "Handoff summary is required."},
            "Error: Handoff summary is required.",
            force_json=force_json,
            stdout_stream=stdout_stream,
        )
        return EXIT_ERROR

    runtime = detect_runtime_context(cwd=config.workspace_root, env=config.env)
    branch = getattr(args, "branch", None) or detect_git_branch(cwd=config.workspace_root, env=config.env)
    commit_sha = detect_git_commit_sha(cwd=config.workspace_root, env=config.env)
    branch_state = get_branch_state(config, branch=branch)
    task_id = getattr(args, "task", None) or branch_state.get("taskId")
    repo = getattr(args, "repo", None) or config.repository or runtime["repository"]
    topic = getattr(args, "topic", None) or f"{repo} ({branch})"

    idempotency_key = compute_idempotency_key(
        org_id=config.organization_id or "",
        repo=repo,
        branch=branch,
        commit_sha=commit_sha,
        tag="HANDOFF",
        summary=summary,
    )

    seam_body: dict[str, Any] = {
        "tag": "HANDOFF",
        "summary": summary,
        "repository": repo,
        "gitBranch": branch,
        "commitSha": commit_sha,
        "taskId": task_id,
        "agentId": runtime["agentId"],
        "modelName": runtime["modelName"],
        "idempotencyKey": idempotency_key,
        "metadata": {
            "topic": topic,
            "idempotencyKey": idempotency_key,
        },
    }
    if config.organization_id:
        seam_body["organizationId"] = config.organization_id

    seam_resp = client.request(
        "POST",
        "/api/v1/fleet/seam",
        json_body=seam_body,
        idempotency_key=idempotency_key,
        spool_on_offline=True,
    )

    # Also project to working-memory scratchpad
    scratch_key = compute_idempotency_key(
        org_id=config.organization_id or "",
        repo=repo,
        branch=branch,
        commit_sha=commit_sha,
        tag=f"SCRATCHPAD:{topic}",
        summary=summary,
    )
    scratch_body: dict[str, Any] = {
        "topic": topic,
        "body": summary,
        "bodyMarkdown": summary,
        "repository": repo,
        "gitBranch": branch,
        "agentId": runtime["agentId"],
    }
    if config.organization_id:
        scratch_body["organizationId"] = config.organization_id
    client.request(
        "POST",
        "/api/v1/fleet/scratchpad",
        json_body=scratch_body,
        idempotency_key=scratch_key,
        spool_on_offline=True,
    )

    out = {
        **seam_resp.data,
        "tag": "HANDOFF",
        "topic": topic,
        "summary": summary,
        "taskId": task_id,
        "idempotencyKey": idempotency_key,
    }
    human = (
        f"[SPOOLED] Handoff queued ({topic}): {summary}"
        if seam_resp.spooled
        else f"[HANDOFF] ({topic}): {summary}"
        if seam_resp.ok
        else f"Error recording handoff: {seam_resp.error}"
    )
    emit_output(out, human, force_json=force_json, stdout_stream=stdout_stream)
    return seam_resp.exit_code


def handle_memory_list(
    _args: Any,
    _config: BridgeConfig,
    client: BridgeClient,
    *,
    force_json: bool = False,
    stdout_stream: Any = None,
) -> int:
    """List workspace Company Memory pins, automations, and scopes from GET /api/v1/ingest/topology."""
    resp = client.request("GET", "/api/v1/ingest/topology")
    pins = resp.data.get("pins", []) if isinstance(resp.data.get("pins"), list) else []
    rows = [
        [
            p.get("id", ""),
            p.get("provider", "custom"),
            p.get("resourceType", "DOC"),
            (p.get("title") or "")[:44],
            (p.get("url") or "")[:44],
        ]
        for p in pins
        if isinstance(p, dict)
    ]
    table = format_table(["ID", "PROVIDER", "TYPE", "TITLE", "URL"], rows)
    emit_output(resp.data, table, force_json=force_json, stdout_stream=stdout_stream)
    return resp.exit_code


def handle_memory_pin(
    args: Any,
    config: BridgeConfig,
    client: BridgeClient,
    *,
    force_json: bool = False,
    stdin_stream: Any = None,
    stdout_stream: Any = None,
) -> int:
    """Upsert a rich Markdown Pin & KnowledgeChunk in Company Memory via POST /api/v1/ingest/topology."""
    title = str(getattr(args, "title", "") or "").strip()
    if not title:
        emit_output(
            {"ok": False, "error": "TITLE_REQUIRED", "message": "Pin title is required."},
            "Error: Pin title is required.",
            force_json=force_json,
            stdout_stream=stdout_stream,
        )
        return EXIT_ERROR

    file_arg = getattr(args, "file", None)
    if file_arg:
        body_markdown = Path(file_arg).read_text(encoding="utf-8").strip()
    else:
        body_markdown = resolve_stdin_or_arg(getattr(args, "body", None), stdin_stream=stdin_stream) or ""

    provider = str(getattr(args, "provider", None) or "bridge").strip().lower()
    resource_type = str(getattr(args, "resource_type", None) or "PLAYBOOK").strip().upper()
    pillar = str(getattr(args, "pillar", None) or "PROCEDURAL_GUARDRAIL").strip().upper()
    slug = title.lower().replace(" ", "-").replace("/", "-")[:48]
    url = str(getattr(args, "url", None) or f"https://bridge.xray.tech/knowledge#{slug}").strip()

    pin_entry: dict[str, Any] = {
        "title": title,
        "url": url,
        "provider": provider,
        "resourceType": resource_type,
        "bodyMarkdown": body_markdown,
        "previewMeta": {
            "pillar": pillar,
            "bodyMarkdown": body_markdown,
        },
    }

    payload_body: dict[str, Any] = {
        "pins": [pin_entry],
    }
    if body_markdown:
        payload_body["chunks"] = [
            {
                "pillar": pillar,
                "sourceType": resource_type,
                "sourceId": f"pin:{provider}:{slug}",
                "title": title,
                "content": body_markdown,
                "citations": [{"provider": provider, "url": url}],
            }
        ]
    if config.organization_id:
        payload_body["organizationId"] = config.organization_id

    branch = detect_git_branch(cwd=config.workspace_root, env=config.env)
    commit_sha = detect_git_commit_sha(cwd=config.workspace_root, env=config.env)
    repo = config.repository or "workspace-repo"
    idempotency_key = compute_idempotency_key(
        org_id=config.organization_id or "",
        repo=repo,
        branch=branch,
        commit_sha=commit_sha,
        tag=f"MEMORY_PIN:{provider}:{url}",
        summary=f"{title}:{body_markdown[:120]}",
    )

    resp = client.request(
        "POST",
        "/api/v1/ingest/topology",
        json_body=payload_body,
        idempotency_key=idempotency_key,
        spool_on_offline=True,
    )
    out = {
        **resp.data,
        "title": title,
        "url": url,
        "provider": provider,
        "pillar": pillar,
        "idempotencyKey": idempotency_key,
    }
    human = (
        f"[SPOOLED] Queued Company Memory pin '{title}' ({provider})."
        if resp.spooled
        else f"Pinned '{title}' ({provider} · {pillar}) to Bridge Company Memory."
        if resp.ok
        else f"Error pinning to Company Memory: {resp.error}"
    )
    emit_output(out, human, force_json=force_json, stdout_stream=stdout_stream)
    return resp.exit_code

