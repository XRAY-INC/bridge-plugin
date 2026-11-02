"""Cross-worktree scope claims (claim, release, claims) and Git worktree isolation."""

from __future__ import annotations

import re
import shutil
import subprocess
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from ..client import BridgeClient
from ..config import BridgeConfig, load_local_claims, save_local_claims
from ..runtime import (
    EXIT_CONCURRENCY_BLOCK,
    EXIT_ERROR,
    EXIT_OK,
    detect_git_branch,
    detect_runtime_context,
    emit_output,
    format_table,
    globs_overlap,
)


def parse_ttl_seconds(raw_ttl: str | int | None, default_seconds: int = 1800) -> int:
    """Parse duration strings like '30m', '45m', '1800s', '1h', or integer minutes/seconds."""
    if raw_ttl is None:
        return default_seconds
    if isinstance(raw_ttl, int):
        return raw_ttl if raw_ttl > 120 else raw_ttl * 60
    text = str(raw_ttl).strip().lower()
    if not text:
        return default_seconds
    match = re.match(r"^(\d+)\s*([smhd]?)$", text)
    if not match:
        return default_seconds
    val = int(match.group(1))
    unit = match.group(2)
    if unit == "s":
        return max(10, val)
    if unit == "m" or unit == "":
        return max(60, val * 60)
    if unit == "h":
        return val * 3600
    if unit == "d":
        return val * 86400
    return default_seconds


def handle_claim(
    args: Any,
    config: BridgeConfig,
    client: BridgeClient,
    *,
    force_json: bool = False,
    stdout_stream: Any = None,
) -> int:
    """Claim one or more file/directory globs across worktrees; exits with code 2 on overlap conflict."""
    raw_globs = getattr(args, "globs", []) or []
    globs: list[str] = []
    for item in raw_globs:
        for part in str(item).split(","):
            if part.strip():
                globs.append(part.strip())

    if not globs:
        emit_output(
            {"ok": False, "error": "GLOBS_REQUIRED", "message": "Provide at least one file or directory glob."},
            "Error: Provide at least one file or directory glob.",
            force_json=force_json,
            stdout_stream=stdout_stream,
        )
        return EXIT_ERROR

    runtime = detect_runtime_context(
        cwd=config.workspace_root,
        explicit_agent=getattr(args, "agent", None),
    )
    agent_id = runtime["agentId"]
    branch = getattr(args, "branch", None) or detect_git_branch(cwd=config.workspace_root)
    repo = getattr(args, "repo", None) or config.repository or runtime["repository"]
    ttl_seconds = parse_ttl_seconds(getattr(args, "ttl", None))
    reason = getattr(args, "reason", None)

    # 1. Check local claims cache (.bridge/claims.json) for conflicts with OTHER agents
    local_claims = load_local_claims(config)
    for existing in local_claims:
        holder = str(existing.get("agentId") or "")
        if holder and holder != agent_id and existing.get("repository") == repo:
            for existing_glob in existing.get("claimedGlobs", []):
                for requested_glob in globs:
                    if globs_overlap(requested_glob, existing_glob):
                        conflict_payload = {
                            "ok": False,
                            "error": "LEASE_CONFLICT",
                            "message": f"Glob '{requested_glob}' overlaps with active claim '{existing_glob}' held by agent '{holder}'.",
                            "conflictingLease": existing,
                        }
                        emit_output(
                            conflict_payload,
                            f"CONFLICT: Glob '{requested_glob}' is already claimed by '{holder}' ({existing_glob}).",
                            force_json=force_json,
                            stdout_stream=stdout_stream,
                        )
                        return EXIT_CONCURRENCY_BLOCK

    # 2. If authenticated/bound and online, synchronize with /api/v1/fleet/leases
    remote_lease: dict[str, Any] | None = None
    if config.is_authenticated or config.is_bound:
        body: dict[str, Any] = {
            "action": "claim",
            "repository": repo,
            "worktreeBranch": branch,
            "claimedGlobs": globs,
            "agentId": agent_id,
            "modelName": runtime["modelName"],
            "reason": reason,
            "ttlSeconds": ttl_seconds,
        }
        if config.organization_id:
            body["organizationId"] = config.organization_id
        if config.user_email:
            body["userEmail"] = config.user_email

        resp = client.request("POST", "/api/v1/fleet/leases", json_body=body, require_auth=False)
        if resp.exit_code == EXIT_CONCURRENCY_BLOCK or resp.status_code == 409:
            emit_output(
                resp.data,
                f"CONFLICT: {resp.data.get('message') or resp.error}",
                force_json=force_json,
                stdout_stream=stdout_stream,
            )
            return EXIT_CONCURRENCY_BLOCK
        if resp.ok and isinstance(resp.data.get("lease"), dict):
            remote_lease = resp.data["lease"]

    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(seconds=ttl_seconds)
    lease_record = remote_lease or {
        "id": f"claim_{uuid.uuid4().hex[:10]}",
        "repository": repo,
        "worktreeBranch": branch,
        "claimedGlobs": globs,
        "agentId": agent_id,
        "reason": reason,
        "createdAt": now.isoformat(),
        "expiresAt": expires_at.isoformat(),
    }
    lease_record["expiresAtEpoch"] = time.time() + ttl_seconds
    local_claims.append(lease_record)
    save_local_claims(config, local_claims)

    payload = {
        "ok": True,
        "lease": lease_record,
        "claimedGlobs": globs,
        "agentId": agent_id,
        "ttlSeconds": ttl_seconds,
    }
    emit_output(
        payload,
        f"Claimed {', '.join(globs)} for agent '{agent_id}' on {repo}:{branch} (TTL: {ttl_seconds // 60}m).",
        force_json=force_json,
        stdout_stream=stdout_stream,
    )
    return EXIT_OK


def handle_release(
    args: Any,
    config: BridgeConfig,
    client: BridgeClient,
    *,
    force_json: bool = False,
    stdout_stream: Any = None,
) -> int:
    """Release active scope claims held by this agent (or by lease ID / --all)."""
    runtime = detect_runtime_context(
        cwd=config.workspace_root,
        explicit_agent=getattr(args, "agent", None),
    )
    agent_id = runtime["agentId"]
    lease_id = getattr(args, "lease_id", None)
    release_all = bool(getattr(args, "all", False))
    repo = getattr(args, "repo", None) or config.repository or runtime["repository"]

    local_claims = load_local_claims(config)
    kept: list[dict[str, Any]] = []
    released_local = 0
    for item in local_claims:
        if lease_id and item.get("id") == lease_id:
            released_local += 1
            continue
        if release_all and (item.get("agentId") == agent_id or not getattr(args, "agent", None)):
            released_local += 1
            continue
        if not lease_id and not release_all and item.get("agentId") == agent_id:
            released_local += 1
            continue
        kept.append(item)
    save_local_claims(config, kept)

    remote_released = 0
    if config.is_authenticated or config.is_bound:
        body: dict[str, Any] = {
            "action": "release",
            "repository": repo,
            "agentId": None if release_all else agent_id,
            "leaseId": lease_id,
            "all": release_all,
        }
        if config.organization_id:
            body["organizationId"] = config.organization_id
        resp = client.request("POST", "/api/v1/fleet/leases", json_body=body, require_auth=False)
        if resp.ok:
            remote_released = int(resp.data.get("releasedCount", 0) or 0)

    total_released = max(released_local, remote_released)
    payload = {
        "ok": True,
        "released": True,
        "releasedCount": total_released,
        "remainingLocalCount": len(kept),
    }
    emit_output(
        payload,
        f"Released {total_released} scope claim(s) ({len(kept)} remaining active).",
        force_json=force_json,
        stdout_stream=stdout_stream,
    )
    return EXIT_OK


def handle_claims_list(
    args: Any,
    config: BridgeConfig,
    client: BridgeClient,
    *,
    force_json: bool = False,
    stdout_stream: Any = None,
) -> int:
    """List active scope claims from local cache and /api/v1/fleet/leases."""
    local_claims = load_local_claims(config)
    remote_leases: list[dict[str, Any]] = []

    if config.is_authenticated or config.is_bound:
        params: dict[str, Any] = {}
        if getattr(args, "repo", None) or config.repository:
            params["repository"] = getattr(args, "repo", None) or config.repository
        resp = client.request("GET", "/api/v1/fleet/leases", params=params, require_auth=False)
        if resp.ok and isinstance(resp.data.get("leases"), list):
            remote_leases = [l for l in resp.data["leases"] if isinstance(l, dict)]

    by_id: dict[str, dict[str, Any]] = {}
    for item in [*local_claims, *remote_leases]:
        lid = str(item.get("id") or uuid.uuid4().hex[:8])
        by_id[lid] = item

    merged = list(by_id.values())
    rows = [
        [
            c.get("id", ""),
            c.get("agentId", ""),
            c.get("repository", ""),
            c.get("worktreeBranch", ""),
            ",".join(c.get("claimedGlobs", [])) if isinstance(c.get("claimedGlobs"), list) else "",
            str(c.get("expiresAt", ""))[:19],
        ]
        for c in merged
    ]
    payload = {"ok": True, "leases": merged, "count": len(merged)}
    table = format_table(["ID", "AGENT", "REPO", "BRANCH", "GLOBS", "EXPIRES_AT"], rows)
    emit_output(payload, table, force_json=force_json, stdout_stream=stdout_stream)
    return EXIT_OK


def handle_worktree(
    args: Any,
    config: BridgeConfig,
    *,
    force_json: bool = False,
    stdout_stream: Any = None,
) -> int:
    """List, create, or remove isolated Git worktrees while hydrating .bridge.json."""
    sub = str(getattr(args, "worktree_action", "list")).strip().lower()
    root = config.workspace_root

    if sub == "list":
        proc = subprocess.run(
            ["git", "worktree", "list", "--porcelain"],
            cwd=str(root),
            capture_output=True,
            text=True,
            check=False,
        )
        worktrees: list[dict[str, str]] = []
        current: dict[str, str] = {}
        for line in proc.stdout.splitlines():
            if not line.strip():
                if current:
                    worktrees.append(current)
                    current = {}
                continue
            if line.startswith("worktree "):
                current["path"] = line[len("worktree "):].strip()
            elif line.startswith("HEAD "):
                current["head"] = line[len("HEAD "):].strip()
            elif line.startswith("branch "):
                current["branch"] = line[len("branch "):].strip().replace("refs/heads/", "")
        if current:
            worktrees.append(current)

        rows = [[w.get("path", ""), w.get("branch", "detached"), w.get("head", "")[:8]] for w in worktrees]
        emit_output(
            {"ok": True, "worktrees": worktrees},
            format_table(["PATH", "BRANCH", "HEAD"], rows),
            force_json=force_json,
            stdout_stream=stdout_stream,
        )
        return EXIT_OK

    if sub == "create":
        slug = str(getattr(args, "slug", "")).strip()
        if not slug:
            emit_output(
                {"ok": False, "error": "SLUG_REQUIRED"},
                "Error: Provide a worktree slug.",
                force_json=force_json,
                stdout_stream=stdout_stream,
            )
            return EXIT_ERROR
        branch = getattr(args, "branch", None) or f"wt/{slug}"
        wt_path = root / ".worktrees" / slug
        wt_path.parent.mkdir(parents=True, exist_ok=True)
        proc = subprocess.run(
            ["git", "worktree", "add", "-b", branch, str(wt_path)],
            cwd=str(root),
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            subprocess.run(
                ["git", "worktree", "add", str(wt_path), branch],
                cwd=str(root),
                capture_output=True,
                text=True,
                check=False,
            )
        # Hydrate .bridge.json into the linked worktree if present
        if config.config_path and config.config_path.is_file() and wt_path.is_dir():
            shutil.copy2(config.config_path, wt_path / config.config_path.name)

        payload = {
            "ok": True,
            "slug": slug,
            "branch": branch,
            "worktreePath": str(wt_path),
        }
        emit_output(
            payload,
            f"Created isolated worktree at {wt_path} (branch: {branch}).",
            force_json=force_json,
            stdout_stream=stdout_stream,
        )
        return EXIT_OK

    if sub in ("rm", "remove"):
        slug = str(getattr(args, "slug", "")).strip()
        wt_path = root / ".worktrees" / slug
        subprocess.run(
            ["git", "worktree", "remove", "--force", str(wt_path)],
            cwd=str(root),
            capture_output=True,
            text=True,
            check=False,
        )
        if wt_path.exists():
            shutil.rmtree(wt_path, ignore_errors=True)
        emit_output(
            {"ok": True, "removed": str(wt_path)},
            f"Removed worktree {wt_path}.",
            force_json=force_json,
            stdout_stream=stdout_stream,
        )
        return EXIT_OK

    return EXIT_ERROR
