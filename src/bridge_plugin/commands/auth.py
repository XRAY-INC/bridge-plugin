"""Authentication (login/whoami/logout), repository init (.bridge.json), status, and multi-harness setup."""

from __future__ import annotations

import json
import os
import shutil
import stat
from dataclasses import replace
from pathlib import Path
from typing import Any

from ..client import BridgeClient
from ..config import (
    BridgeConfig,
    clear_user_credentials,
    decode_api_key_claims,
    get_branch_state,
    load_local_claims,
    save_user_credentials,
    write_project_config,
)
from ..runtime import (
    DEFAULT_API_URL,
    EXIT_AUTH_MISSING,
    EXIT_ERROR,
    EXIT_OK,
    detect_git_branch,
    detect_git_repo_slug,
    detect_runtime_context,
    emit_output,
    format_table,
    resolve_stdin_or_arg,
)


def handle_auth_login(
    args: Any,
    config: BridgeConfig,
    client: BridgeClient,
    *,
    force_json: bool = False,
    stdin_stream: Any = None,
    stdout_stream: Any = None,
) -> int:
    """Authenticate with Bridge and persist credentials in ~/.config/bridge/credentials.json."""
    raw_key = getattr(args, "api_key", None)
    if raw_key == "-":
        api_key = resolve_stdin_or_arg("-", stdin_stream=stdin_stream)
    elif raw_key:
        api_key = str(raw_key).strip()
    else:
        api_key = (config.api_key or "").strip()

    if not api_key:
        emit_output(
            {
                "ok": False,
                "error": "API_KEY_REQUIRED",
                "message": "Provide --api-key <xrkey_v1...>, pipe via '--api-key -', or set BRIDGE_API_KEY.",
            },
            "Error: Provide --api-key <xrkey_v1...> or set BRIDGE_API_KEY.",
            force_json=force_json,
            stdout_stream=stdout_stream,
        )
        return EXIT_AUTH_MISSING

    if not api_key.startswith("xrkey_v1."):
        emit_output(
            {
                "ok": False,
                "error": "INVALID_API_KEY",
                "message": "That is not a Bridge API key. Keys start with xrkey_v1. Create one in Settings.",
            },
            "Error: That is not a Bridge API key. Keys start with xrkey_v1. Create one in Settings.",
            force_json=force_json,
            stdout_stream=stdout_stream,
        )
        return EXIT_AUTH_MISSING

    api_url = (getattr(args, "url", None) or config.api_url or DEFAULT_API_URL).strip()
    org_id = (
        getattr(args, "org", None)
        or decode_api_key_claims(api_key).get("organizationId")
        or config.organization_id
    )
    user_email = getattr(args, "email", None) or config.user_email

    # A key Bridge rejects is never saved. Being offline is not a rejection, so that case still saves.
    verify_client = BridgeClient(
        replace(config, api_key=api_key, api_url=api_url, organization_id=org_id),
        agent_id=client.agent_id,
        timeout=client.timeout,
        transport=client._transport,
        env=client._env,
    )
    check = verify_client.request("GET", "/api/v1/fleet/api-keys", require_auth=False)
    if check.status_code in (401, 403):
        emit_output(
            {
                "ok": False,
                "authenticated": False,
                "error": "INVALID_API_KEY",
                "message": "Bridge rejected this API key. It was not saved.",
                "serverError": check.error,
            },
            "Error: Bridge rejected this API key. It was not saved.",
            force_json=force_json,
            stdout_stream=stdout_stream,
        )
        return EXIT_AUTH_MISSING

    cred_path = config.credentials_path or save_user_credentials(
        api_key=api_key,
        api_url=api_url,
        organization_id=org_id,
        user_email=user_email,
    )
    if config.credentials_path:
        save_user_credentials(
            api_key=api_key,
            api_url=api_url,
            organization_id=org_id,
            user_email=user_email,
            env={"BRIDGE_CREDENTIALS_PATH": str(config.credentials_path)},
        )
    key_hint = f"{api_key[:14]}...{api_key[-4:]}" if len(api_key) > 18 else f"{api_key[:4]}..."
    payload = {
        "ok": True,
        "authenticated": True,
        "verified": check.ok,
        "keyHint": key_hint,
        "organizationId": org_id,
        "apiUrl": api_url,
        "credentialsPath": str(cred_path),
    }
    emit_output(
        payload,
        [
            f"Authenticated with Bridge ({api_url})",
            f"  Key Hint        : {key_hint}",
            f"  Organization ID : {org_id or '(unknown)'}",
            f"  Saved To        : {cred_path}",
            *(
                []
                if check.ok
                else ["  Warning         : Could not reach Bridge to check this key. Run 'bridge auth whoami --verify'."]
            ),
        ],
        force_json=force_json,
        stdout_stream=stdout_stream,
    )
    return EXIT_OK


def handle_auth_whoami(
    args: Any,
    config: BridgeConfig,
    client: BridgeClient,
    *,
    force_json: bool = False,
    stdout_stream: Any = None,
) -> int:
    """Display active Bridge authentication and verify against /api/v1/fleet/api-keys when online."""
    runtime = detect_runtime_context(cwd=config.workspace_root)
    if not config.is_authenticated and not config.is_bound:
        emit_output(
            {
                "ok": False,
                "authenticated": False,
                "error": "NOT_AUTHENTICATED",
                "message": "No Bridge API key or .bridge.json found. Run 'bridge auth login' or 'bridge init'.",
            },
            "Not authenticated. Run 'bridge auth login --api-key <key>' or export BRIDGE_API_KEY.",
            force_json=force_json,
            stdout_stream=stdout_stream,
        )
        return EXIT_AUTH_MISSING

    verify_data: dict[str, Any] = {}
    verified: bool | None = None
    if getattr(args, "verify", False):
        resp = client.request("GET", "/api/v1/fleet/api-keys", require_auth=False)
        verified = resp.ok
        verify_data = resp.data if resp.ok else {}
        if not resp.ok:
            rejected = resp.status_code in (401, 403)
            message = (
                "Bridge rejected this API key. Create a new one in Settings, then run 'bridge auth login'."
                if rejected
                else "Could not reach Bridge to check this API key."
            )
            emit_output(
                {
                    "ok": False,
                    "authenticated": False if rejected else config.is_authenticated,
                    "verified": False,
                    "error": "INVALID_API_KEY" if rejected else (resp.error or "VERIFY_FAILED"),
                    "message": message,
                    "keyHint": config.key_hint,
                    "apiUrl": config.api_url,
                },
                f"Error: {message}",
                force_json=force_json,
                stdout_stream=stdout_stream,
            )
            return EXIT_AUTH_MISSING if rejected else EXIT_ERROR

    payload = {
        "ok": True,
        "authenticated": config.is_authenticated,
        "verified": verified,
        "authSource": config.auth_source,
        "keyHint": config.key_hint,
        "organizationId": verify_data.get("organizationId")
        or config.organization_id
        or decode_api_key_claims(config.api_key).get("organizationId"),
        "apiUrl": config.api_url,
        "repository": config.repository,
        "userEmail": config.user_email,
        "agentId": runtime["agentId"],
        "configPath": str(config.config_path) if config.config_path else None,
    }
    emit_output(
        payload,
        [
            "Bridge Identity",
            f"  Authenticated   : {'yes' if config.is_authenticated else 'no (project-bound only)'}",
            f"  Auth Source     : {config.auth_source}",
            f"  Key Hint        : {config.key_hint or '(none)'}",
            f"  Organization ID : {payload['organizationId'] or '(unset)'}",
            f"  API URL         : {config.api_url}",
            f"  Repository      : {config.repository}",
            f"  Agent Runtime   : {runtime['agentId']}",
        ],
        force_json=force_json,
        stdout_stream=stdout_stream,
    )
    return EXIT_OK


def handle_auth_logout(
    _args: Any,
    config: BridgeConfig,
    *,
    force_json: bool = False,
    stdout_stream: Any = None,
) -> int:
    """Remove saved user credentials from ~/.config/bridge/credentials.json."""
    env_override = (
        {"BRIDGE_CREDENTIALS_PATH": str(config.credentials_path)}
        if config.credentials_path
        else None
    )
    removed = clear_user_credentials(env=env_override)
    payload = {"ok": True, "removed": removed}
    emit_output(
        payload,
        "Removed saved Bridge credentials." if removed else "No saved Bridge credentials found.",
        force_json=force_json,
        stdout_stream=stdout_stream,
    )
    return EXIT_OK


def _ignore_local_state(root: Path) -> bool:
    """Keeps Bridge's per-machine state out of commits. Returns whether .gitignore changed."""
    gitignore = root / ".gitignore"
    if not gitignore.exists() and not (root / ".git").exists():
        return False
    existing = gitignore.read_text(encoding="utf-8") if gitignore.exists() else ""
    if any(line.strip().strip("/") == ".bridge" for line in existing.splitlines()):
        return False
    separator = "" if not existing or existing.endswith("\n") else "\n"
    gitignore.write_text(f"{existing}{separator}.bridge/\n", encoding="utf-8")
    return True


def handle_init(
    args: Any,
    config: BridgeConfig,
    *,
    force_json: bool = False,
    stdout_stream: Any = None,
) -> int:
    """Bind the current repository to Bridge by writing .bridge.json."""
    org_id = (
        getattr(args, "org", None)
        or config.organization_id
        or decode_api_key_claims(config.api_key).get("organizationId")
        or ""
    ).strip()
    if not org_id:
        emit_output(
            {
                "ok": False,
                "error": "ORG_ID_REQUIRED",
                "message": "Run 'bridge auth login --api-key <key>' first, or provide --org <organizationId>.",
            },
            "Error: Run 'bridge auth login --api-key <key>' first, or provide --org <organizationId>.",
            force_json=force_json,
            stdout_stream=stdout_stream,
        )
        return EXIT_ERROR

    repo = (
        getattr(args, "repo", None)
        or config.repository
        or detect_git_repo_slug(cwd=config.workspace_root)
    ).strip()
    api_url = (getattr(args, "url", None) or config.api_url or DEFAULT_API_URL).strip()

    cfg_path = write_project_config(
        config.workspace_root,
        organization_id=org_id,
        repository=repo,
        api_url=api_url,
    )
    gitignore_updated = _ignore_local_state(config.workspace_root)
    payload = {
        "ok": True,
        "plugin": "bridge",
        "gitignoreUpdated": gitignore_updated,
        "configPath": str(cfg_path),
        "organizationId": org_id,
        "repository": repo,
        "apiUrl": api_url,
        "apiKeyEnv": "BRIDGE_API_KEY",
    }
    emit_output(
        payload,
        [
            f"Initialized Bridge project configuration at {cfg_path}",
            f"  Organization ID : {org_id}",
            f"  Repository      : {repo}",
            f"  API URL         : {api_url}",
            *(["  Git             : Added .bridge/ to .gitignore"] if gitignore_updated else []),
            "  Next Step       : "
            + ("bridge status" if config.is_authenticated else "bridge auth login --api-key <key>"),
        ],
        force_json=force_json,
        stdout_stream=stdout_stream,
    )
    return EXIT_OK


def handle_status(
    _args: Any,
    config: BridgeConfig,
    client: BridgeClient,
    *,
    force_json: bool = False,
    stdout_stream: Any = None,
) -> int:
    """Inspect repo binding, active branch task/job state, local claims, and offline spool."""
    if not config.is_bound and not config.is_authenticated:
        emit_output(
            {
                "ok": False,
                "error": "AUTH_OR_CONFIG_MISSING",
                "message": "Repository is not bound (.bridge.json missing) and BRIDGE_API_KEY is not set.",
            },
            "Bridge is not configured in this workspace. Run 'bridge init --org <orgId>' or set BRIDGE_API_KEY.",
            force_json=force_json,
            stdout_stream=stdout_stream,
        )
        return EXIT_AUTH_MISSING

    runtime = detect_runtime_context(cwd=config.workspace_root)
    branch = detect_git_branch(cwd=config.workspace_root)
    branch_state = get_branch_state(config, branch=branch)
    local_claims = load_local_claims(config)
    spooled_items = client.read_spool()

    payload = {
        "ok": True,
        "plugin": "bridge",
        "organizationId": config.organization_id,
        "repository": config.repository,
        "apiUrl": config.api_url,
        "authenticated": config.is_authenticated,
        "authSource": config.auth_source,
        "keyHint": config.key_hint,
        "configPath": str(config.config_path) if config.config_path else None,
        "gitBranch": branch,
        "commitSha": runtime["commitSha"],
        "branchState": branch_state,
        "runtime": runtime,
        "localClaimsCount": len(local_claims),
        "localClaims": local_claims,
        "spoolCount": len(spooled_items),
        "spoolPath": str(config.spool_path),
    }

    human_lines = [
        "Bridge Workspace Status",
        f"  Organization : {config.organization_id or '(from API key)'}",
        f"  Repository   : {config.repository}",
        f"  API URL      : {config.api_url}",
        f"  Auth         : {config.auth_source} ({config.key_hint or 'unauthenticated'})",
        f"  Branch       : {branch} ({(runtime['commitSha'] or 'no-commit')[:7]})",
        f"  Bound Task   : {branch_state.get('taskId') or '(none)'}",
        f"  Bound Job    : {branch_state.get('jobId') or '(none)'}",
        f"  Agent / CI   : {runtime['agentId']}"
        + (f" [{runtime['ci']['provider']}]" if runtime["ci"]["provider"] else ""),
        f"  Scope Claims : {len(local_claims)} active",
        f"  Offline Spool: {len(spooled_items)} queued",
    ]
    emit_output(payload, human_lines, force_json=force_json, stdout_stream=stdout_stream)
    return EXIT_OK


def _package_skill_markdown() -> str:
    skill_file = Path(__file__).resolve().parents[3] / "skills" / "bridge" / "SKILL.md"
    if skill_file.is_file():
        return skill_file.read_text(encoding="utf-8")
    return (
        "---\n"
        "name: bridge\n"
        "description: Connect AI coding agents to Bridge (https://bridge.xray.tech) via bridge / brg and bridge-mcp.\n"
        "---\n\n"
        "# Bridge Agent Skill\n\n"
        "Run `bridge status --json` or read MCP resource `bridge://context` at session start.\n"
    )


def handle_setup(
    args: Any,
    config: BridgeConfig,
    *,
    force_json: bool = False,
    stdout_stream: Any = None,
) -> int:
    """Install MCP server configs, SKILL.md, or native git hooks for the selected target."""
    target = str(getattr(args, "target", "")).strip().lower()
    root = config.workspace_root
    files_written: list[str] = []

    mcp_entry = {
        "command": "bridge-mcp",
        "args": [],
    }

    if target == "claude":
        mcp_path = root / ".mcp.json"
        existing = {}
        if mcp_path.is_file():
            try:
                existing = json.loads(mcp_path.read_text(encoding="utf-8"))
            except Exception:
                existing = {}
        existing.setdefault("mcpServers", {})["bridge"] = mcp_entry
        mcp_path.write_text(json.dumps(existing, indent=2) + "\n", encoding="utf-8")
        files_written.append(str(mcp_path))

        skill_dest = root / ".claude" / "skills" / "bridge" / "SKILL.md"
        skill_dest.parent.mkdir(parents=True, exist_ok=True)
        skill_dest.write_text(_package_skill_markdown(), encoding="utf-8")
        files_written.append(str(skill_dest))

    elif target == "cursor":
        cursor_mcp = root / ".cursor" / "mcp.json"
        cursor_mcp.parent.mkdir(parents=True, exist_ok=True)
        existing = {}
        if cursor_mcp.is_file():
            try:
                existing = json.loads(cursor_mcp.read_text(encoding="utf-8"))
            except Exception:
                existing = {}
        existing.setdefault("mcpServers", {})["bridge"] = mcp_entry
        cursor_mcp.write_text(json.dumps(existing, indent=2) + "\n", encoding="utf-8")
        files_written.append(str(cursor_mcp))

    elif target == "antigravity":
        agy_skill = root / ".agent" / "skills" / "bridge" / "SKILL.md"
        agy_skill.parent.mkdir(parents=True, exist_ok=True)
        agy_skill.write_text(_package_skill_markdown(), encoding="utf-8")
        files_written.append(str(agy_skill))

        gemini_settings = root / ".gemini" / "settings.json"
        gemini_settings.parent.mkdir(parents=True, exist_ok=True)
        existing = {}
        if gemini_settings.is_file():
            try:
                existing = json.loads(gemini_settings.read_text(encoding="utf-8"))
            except Exception:
                existing = {}
        existing.setdefault("mcpServers", {})["bridge"] = mcp_entry
        gemini_settings.write_text(json.dumps(existing, indent=2) + "\n", encoding="utf-8")
        files_written.append(str(gemini_settings))

    elif target == "codex":
        codex_skill = root / ".agents" / "skills" / "bridge" / "SKILL.md"
        codex_skill.parent.mkdir(parents=True, exist_ok=True)
        codex_skill.write_text(_package_skill_markdown(), encoding="utf-8")
        files_written.append(str(codex_skill))

    elif target == "git-hooks":
        hooks_dir = root / ".git" / "hooks"
        hooks_dir.mkdir(parents=True, exist_ok=True)

        prep_hook = hooks_dir / "prepare-commit-msg"
        prep_script = (
            "#!/usr/bin/env sh\n"
            "# Bridge prepare-commit-msg hook: appends Bridge-Task trailer when branch is bound\n"
            'COMMIT_MSG_FILE="$1"\n'
            'if [ -f ".bridge/state.json" ] && command -v python3 >/dev/null 2>&1; then\n'
            '  TASK_ID=$(python3 -c \'import json, subprocess; '
            'b=subprocess.run(["git","rev-parse","--abbrev-ref","HEAD"],capture_output=True,text=True).stdout.strip(); '
            's=json.load(open(".bridge/state.json")); '
            'print(s.get("branches",{}).get(b,{}).get("taskId") or "")\' 2>/dev/null)\n'
            '  if [ -n "$TASK_ID" ] && ! grep -q "Bridge-Task: $TASK_ID" "$COMMIT_MSG_FILE"; then\n'
            '    printf "\\nBridge-Task: %s\\n" "$TASK_ID" >> "$COMMIT_MSG_FILE"\n'
            "  fi\n"
            "fi\n"
        )
        prep_hook.write_text(prep_script, encoding="utf-8")
        os.chmod(prep_hook, os.stat(prep_hook).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        files_written.append(str(prep_hook))

        post_hook = hooks_dir / "post-commit"
        post_script = (
            "#!/usr/bin/env sh\n"
            "# Bridge post-commit hook: non-blocking commit signal\n"
            "if command -v brg >/dev/null 2>&1; then\n"
            '  MSG=$(git log -1 --pretty=%s 2>/dev/null)\n'
            '  SHA=$(git rev-parse HEAD 2>/dev/null)\n'
            '  brg seam win "$MSG" --commit "$SHA" >/dev/null 2>&1 || true\n'
            "fi\n"
        )
        post_hook.write_text(post_script, encoding="utf-8")
        os.chmod(post_hook, os.stat(post_hook).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        files_written.append(str(post_hook))
    else:
        emit_output(
            {
                "ok": False,
                "error": "INVALID_SETUP_TARGET",
                "message": f"Unsupported setup target '{target}'. Choose from: claude, cursor, antigravity, codex, git-hooks.",
            },
            f"Error: Unsupported setup target '{target}'.",
            force_json=force_json,
            stdout_stream=stdout_stream,
        )
        return EXIT_ERROR

    payload = {
        "ok": True,
        "target": target,
        "filesWritten": files_written,
    }
    emit_output(
        payload,
        [
            f"Configured Bridge integration for '{target}':",
            *[f"  - {fp}" for fp in files_written],
        ],
        force_json=force_json,
        stdout_stream=stdout_stream,
    )
    return EXIT_OK
