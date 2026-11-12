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
    load_user_credentials,
    normalize_team_roster,
    resolve_user_identity,
    save_cached_roster,
    save_session_user,
    save_user_credentials,
    save_user_identity_credentials,
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
from .statusbar import format_status_bar_line


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
        replace(config, api_key=api_key, api_url=api_url, organization_id=org_id, work_bound=True),
        agent_id=client.agent_id,
        timeout=client.timeout,
        transport=client._transport,
        env=client._env,
    )
    check = verify_client.request("GET", "/api/v1/fleet/api-keys", require_auth=False, allow_unbound=True)
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
    key_label = decode_api_key_claims(api_key).get("label")
    payload = {
        "ok": True,
        "authenticated": True,
        "verified": check.ok,
        "keyHint": key_hint,
        "keyLabel": key_label if isinstance(key_label, str) and key_label.strip() else None,
        "organizationId": org_id,
        "apiUrl": api_url,
        "credentialsPath": str(cred_path),
    }
    emit_output(
        payload,
        [
            f"Authenticated with Bridge ({api_url})",
            f"  Key Hint        : {key_hint}",
            *(
                [f"  Key Name        : {payload['keyLabel']}"]
                if payload["keyLabel"]
                else []
            ),
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
    runtime = detect_runtime_context(cwd=config.workspace_root, env=config.env)
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
        resp = client.request("GET", "/api/v1/fleet/api-keys", require_auth=False, allow_unbound=True)
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

    creds_env = {"BRIDGE_CREDENTIALS_PATH": str(config.credentials_path)} if config.credentials_path else config.env
    saved_creds = load_user_credentials(env=creds_env)
    effective_agent = config.key_label if runtime["agentId"] == "bridge-cli" and config.key_label else runtime["agentId"]
    payload = {
        "ok": True,
        "authenticated": config.is_authenticated,
        "verified": verified,
        "workContext": config.is_work_context,
        "authSource": config.auth_source,
        "keyHint": config.key_hint,
        "keyLabel": config.key_label,
        "organizationId": verify_data.get("organizationId")
        or config.organization_id
        or decode_api_key_claims(config.api_key).get("organizationId")
        or saved_creds.get("organizationId"),
        "apiUrl": config.api_url,
        "repository": config.repository,
        "projectName": config.project_name,
        "clientName": config.client_name,
        "userEmail": config.user_email,
        "userName": config.user_name,
        "userAlias": config.user_alias,
        "userSource": config.user_source,
        "agentId": effective_agent,
        "configPath": str(config.config_path) if config.config_path else None,
    }
    emit_output(
        payload,
        [
            "Bridge Identity",
            f"  Authenticated   : {'yes' if config.is_authenticated else 'no (project-bound only)'}",
            f"  Auth Source     : {config.auth_source}",
            f"  Key Hint        : {config.key_hint or '(none)'}",
            *(
                [f"  Key Name        : {config.key_label}"]
                if config.key_label
                else []
            ),
            *(
                [
                    f"  Teammate        : {config.user_name or config.user_alias or config.user_email}"
                    + (f" <{config.user_email}>" if config.user_email and config.user_name else "")
                    + f" ({config.user_source})"
                ]
                if config.user_email or config.user_alias
                else []
            ),
            f"  Organization ID : {payload['organizationId'] or '(unset)'}",
            f"  API URL         : {config.api_url}",
            f"  Repository      : {config.repository}",
            *([f"  Project         : {config.project_name}"] if config.project_name else []),
            *([f"  Client          : {config.client_name}"] if config.client_name else []),
            f"  Agent / Key     : {effective_agent}",
        ],
        force_json=force_json,
        stdout_stream=stdout_stream,
    )
    return EXIT_OK


def _fetch_server_roster_if_needed(
    config: BridgeConfig,
    client: BridgeClient | None,
) -> dict[str, dict[str, str]]:
    """Return active workspace roster, querying /api/v1/fleet/context once if local roster is empty."""
    roster_map = dict(config.team_roster)
    if roster_map or client is None or not config.is_authenticated:
        return roster_map
    params: dict[str, Any] = {"days": 7}
    if config.organization_id:
        params["organizationId"] = config.organization_id
    resp = client.request("GET", "/api/v1/fleet/context", params=params, require_auth=False)
    if resp.ok and isinstance(resp.data, dict):
        raw_roster = resp.data.get("teamRoster") or []
        if raw_roster:
            roster_map = normalize_team_roster(raw_roster)
            try:
                save_cached_roster(config.workspace_root, raw_roster)
            except Exception:
                pass
    return roster_map


def handle_user(
    args: Any,
    config: BridgeConfig,
    client: BridgeClient | None = None,
    *,
    force_json: bool = False,
    stdout_stream: Any = None,
) -> int:
    """View, set, list workspace roster aliases, or clear active teammate identity for shared-login setups."""
    if getattr(args, "roster", False):
        roster_map = _fetch_server_roster_if_needed(config, client)
        roster_list: list[dict[str, Any]] = []
        rows: list[list[str]] = []
        seen_keys: set[str] = set()
        for alias, entry in sorted(roster_map.items()):
            dedup_key = f"{entry.get('alias')}:{entry.get('email')}"
            if dedup_key in seen_keys:
                continue
            seen_keys.add(dedup_key)
            roster_list.append(dict(entry))
            rows.append([entry.get("alias", alias), entry.get("name", ""), entry.get("email", "")])
        payload = {"ok": True, "roster": roster_list, "count": len(roster_list)}
        human_out = (
            format_table(["ALIAS", "NAME", "EMAIL"], rows)
            if rows
            else "No workspace roster cached yet. Run 'brg context' or configure 'teamRoster' / 'emailDomain' in .bridge.json."
        )
        emit_output(
            payload,
            human_out,
            force_json=force_json,
            stdout_stream=stdout_stream,
        )
        return EXIT_OK

    session_only = bool(getattr(args, "session", False))
    env_override = (
        {"BRIDGE_CREDENTIALS_PATH": str(config.credentials_path)}
        if config.credentials_path
        else None
    )

    if getattr(args, "clear", False):
        if session_only:
            target_path = save_session_user(config.workspace_root, clear=True)
            scope_label = "session"
        else:
            save_session_user(config.workspace_root, clear=True)
            target_path = save_user_identity_credentials(clear=True, env=env_override)
            scope_label = "machine & session"
        payload = {
            "ok": True,
            "cleared": True,
            "scope": scope_label,
            "path": str(target_path),
        }
        emit_output(
            payload,
            f"Cleared Bridge teammate identity ({scope_label}).",
            force_json=force_json,
            stdout_stream=stdout_stream,
        )
        return EXIT_OK

    raw_ident = getattr(args, "identifier", None)
    explicit_email = getattr(args, "email", None)
    explicit_name = getattr(args, "name", None)

    if not raw_ident and not explicit_email:
        # Show current active teammate identity
        has_user = bool(config.user_email or config.user_alias)
        sb_line = format_status_bar_line(config)
        payload = {
            "ok": True,
            "configured": has_user,
            "userAlias": config.user_alias,
            "userName": config.user_name,
            "userEmail": config.user_email,
            "source": config.user_source,
            "statusBar": sb_line,
        }
        if has_user:
            human_lines = [
                f"Active Bridge Teammate: {config.user_name or config.user_alias}"
                + (f" <{config.user_email}>" if config.user_email else ""),
                f"  Alias  : {config.user_alias or '(custom)'}",
                f"  Email  : {config.user_email or '(unset)'}",
                f"  Source : {config.user_source}",
            ]
        else:
            human_lines = [
                "No teammate identity set (using API key owner default).",
                "  Set machine default : brg user <alias|email> (e.g. 'brg user alice@acme.com')",
                "  Set session only    : brg user <alias|email> --session",
                "  View org roster     : brg user --roster",
            ]
        if sb_line:
            human_lines.append(sb_line)
        emit_output(payload, human_lines, force_json=force_json, stdout_stream=stdout_stream)
        return EXIT_OK

    roster_map = dict(config.team_roster)
    if raw_ident and "@" not in str(raw_ident) and str(raw_ident).strip().lower() not in roster_map and not config.email_domain:
        roster_map = _fetch_server_roster_if_needed(config, client)

    resolved = resolve_user_identity(
        raw_ident,
        explicit_email=explicit_email,
        explicit_name=explicit_name,
        roster=roster_map,
        email_domain=config.email_domain,
    )
    if session_only:
        _ignore_local_state(config.workspace_root)
        saved_path = save_session_user(
            config.workspace_root,
            user_email=resolved["email"],
            user_name=resolved["name"],
            user_alias=resolved["alias"],
        )
        scope_label = "session (.bridge/session_user.json)"
    else:
        saved_path = save_user_identity_credentials(
            user_email=resolved["email"],
            user_name=resolved["name"],
            user_alias=resolved["alias"],
            env=env_override,
        )
        scope_label = "machine (~/.config/bridge/credentials.json)"

    updated_cfg = replace(
        config,
        user_email=resolved["email"],
        user_name=resolved["name"],
        user_alias=resolved["alias"],
        user_source="session" if session_only else "credentials",
    )
    sb_line = format_status_bar_line(updated_cfg)
    payload = {
        "ok": True,
        "userAlias": resolved["alias"],
        "userName": resolved["name"],
        "userEmail": resolved["email"],
        "sessionOnly": session_only,
        "savedPath": str(saved_path),
        "statusBar": sb_line,
    }
    email_suffix = f" <{resolved['email']}>" if resolved["email"] else ""
    human_out_lines = [
        f"Active Bridge teammate set to {resolved['name']}{email_suffix}",
        f"  Alias : {resolved['alias']}",
        f"  Scope : {scope_label}",
    ]
    if sb_line:
        human_out_lines.append(sb_line)
    emit_output(
        payload,
        human_out_lines,
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
    creds_env = {"BRIDGE_CREDENTIALS_PATH": str(config.credentials_path)} if config.credentials_path else config.env
    saved_creds = load_user_credentials(env=creds_env)
    org_id = (
        getattr(args, "org", None)
        or config.organization_id
        or decode_api_key_claims(config.api_key).get("organizationId")
        or saved_creds.get("organizationId")
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

    project_name = (
        getattr(args, "project", None)
        or config.project_name
        or ""
    ).strip() or None
    client_name = (
        getattr(args, "client", None)
        or config.client_name
        or ""
    ).strip() or None
    repo = (
        getattr(args, "repo", None)
        or project_name
        or config.repository
        or detect_git_repo_slug(cwd=config.workspace_root)
    ).strip()
    api_url = (getattr(args, "url", None) or config.api_url or DEFAULT_API_URL).strip()
    agency_org_id = (
        getattr(args, "agency_org", None)
        or config.agency_organization_id
        or ""
    ).strip() or None
    if agency_org_id == org_id:
        agency_org_id = None

    cfg_path = write_project_config(
        config.workspace_root,
        organization_id=org_id,
        repository=repo,
        api_url=api_url,
        agency_organization_id=agency_org_id,
        project_name=project_name,
        client_name=client_name,
    )
    gitignore_updated = _ignore_local_state(config.workspace_root)
    auto_setup_files: list[str] = []
    if not getattr(args, "no_setup", False):
        root = config.workspace_root
        for candidate_dir in (".claude", ".cursor", ".agent", ".agents"):
            if (root / candidate_dir).is_dir():
                _install_skill_bundle(root / candidate_dir / "skills", auto_setup_files)

    payload: dict[str, Any] = {
        "ok": True,
        "plugin": "bridge",
        "gitignoreUpdated": gitignore_updated,
        "configPath": str(cfg_path),
        "organizationId": org_id,
        "repository": repo,
        "apiUrl": api_url,
        "apiKeyEnv": "BRIDGE_API_KEY",
    }
    if project_name:
        payload["projectName"] = project_name
    if client_name:
        payload["clientName"] = client_name
    if agency_org_id:
        payload["agencyOrganizationId"] = agency_org_id
    if auto_setup_files:
        payload["skillsUpdated"] = auto_setup_files

    emit_output(
        payload,
        [
            f"Initialized Bridge project configuration at {cfg_path}",
            f"  Organization ID : {org_id}",
            *([f"  Upstream Org    : {agency_org_id}"] if agency_org_id else []),
            f"  Repository      : {repo}",
            *([f"  Project         : {project_name}"] if project_name else []),
            *([f"  Client          : {client_name}"] if client_name else []),
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

    runtime = detect_runtime_context(cwd=config.workspace_root, env=config.env)
    branch = detect_git_branch(cwd=config.workspace_root, env=config.env)
    branch_state = get_branch_state(config, branch=branch)
    local_claims = load_local_claims(config)
    spooled_items = client.read_spool()

    from .safety import load_frozen_globs, load_snapshots
    from .statusbar import format_status_bar_line, is_work_context

    frozen_globs = load_frozen_globs(config)
    snapshots = load_snapshots(config)
    sb_line = format_status_bar_line(config)
    work_bound = is_work_context(config)
    has_org_instructions = config.org_instructions_md_path.is_file()

    payload = {
        "ok": True,
        "plugin": "bridge",
        "workContext": work_bound,
        "organizationId": config.organization_id,
        "agencyOrganizationId": config.agency_organization_id,
        "repository": config.repository,
        "projectName": config.project_name,
        "clientName": config.client_name,
        "userEmail": config.user_email,
        "userName": config.user_name,
        "userAlias": config.user_alias,
        "userSource": config.user_source,
        "apiUrl": config.api_url,
        "authenticated": config.is_authenticated,
        "authSource": config.auth_source,
        "keyHint": config.key_hint,
        "keyLabel": config.key_label,
        "configPath": str(config.config_path) if config.config_path else None,
        "orgInstructionsPath": str(config.org_instructions_md_path) if has_org_instructions else None,
        "gitBranch": branch,
        "commitSha": runtime["commitSha"],
        "branchState": branch_state,
        "runtime": runtime,
        "localClaimsCount": len(local_claims),
        "localClaims": local_claims,
        "frozenGlobs": frozen_globs,
        "savepointsCount": len(snapshots),
        "spoolCount": len(spooled_items),
        "spoolPath": str(config.spool_path),
        "statusBar": sb_line,
    }

    human_lines = [
        "Bridge Workspace Status",
        f"  Scope        : {'WORK (' + str(config.client_name or config.project_name or config.organization_id) + ')' if work_bound else 'LOCAL (unbound / personal — telemetry stays local)'}",
        f"  Organization : {config.organization_id or '(from API key)'}",
        *(
            [f"  Upstream Org : {config.agency_organization_id}"]
            if config.agency_organization_id
            else []
        ),
        f"  Repository   : {config.repository}",
        *([f"  Project      : {config.project_name}"] if config.project_name else []),
        *([f"  Client       : {config.client_name}"] if config.client_name else []),
        f"  API URL      : {config.api_url}",
        f"  Auth         : {config.auth_source} ({config.key_hint or 'unauthenticated'})",
        *(
            [f"  Key Name     : {config.key_label}"]
            if config.key_label
            else []
        ),
        *(
            [
                f"  Teammate     : {config.user_name or config.user_alias or config.user_email}"
                + (f" <{config.user_email}>" if config.user_email and config.user_name else "")
                + f" ({config.user_source})"
            ]
            if config.user_email or config.user_alias
            else []
        ),
        f"  Branch       : {branch} ({(runtime['commitSha'] or 'no-commit')[:7]})",
        f"  Bound Task   : {branch_state.get('taskId') or '(none)'}",
        f"  Bound Job    : {branch_state.get('jobId') or '(none)'}",
        f"  Agent / Key  : {runtime['agentId']}"
        + (f" [{runtime['ci']['provider']}]" if runtime["ci"]["provider"] else ""),
        *(
            [f"  Instructions : {config.org_instructions_md_path} (session-reloaded)"]
            if has_org_instructions
            else []
        ),
        f"  Scope Claims : {len(local_claims)} active",
        f"  Frozen Files : {len(frozen_globs)} pattern(s)"
        + (f" ({', '.join(frozen_globs[:3])})" if frozen_globs else ""),
        f"  Savepoints   : {len(snapshots)} local",
        f"  Offline Spool: {len(spooled_items)} queued",
        *([f"\n{sb_line}"] if sb_line else []),
    ]
    emit_output(payload, human_lines, force_json=force_json, stdout_stream=stdout_stream)
    return EXIT_OK


SKILL_BUNDLE_NAMES = (
    "bridge",
    "bridge-context",
    "bridge-cli",
    "bridge-api",
    "bridge-updates",
    "bridge-afk",
)


def _package_asset_path(*parts: str) -> Path | None:
    bundled = Path(__file__).resolve().parent.parent / "assets" / Path(*parts)
    if bundled.is_file():
        return bundled
    repo_root = Path(__file__).resolve().parents[3]
    fallback = repo_root / Path(*parts)
    if fallback.is_file():
        return fallback
    if len(parts) == 1 and parts[0] == "plugin.json":
        alt = repo_root / ".claude-plugin" / "plugin.json"
        if alt.is_file():
            return alt
    if len(parts) == 1 and parts[0] == "hooks.json":
        alt = repo_root / "hooks" / "hooks.json"
        if alt.is_file():
            return alt
    return None


def _package_skill_markdown(skill_name: str = "bridge") -> str:
    skill_file = _package_asset_path("skills", skill_name, "SKILL.md")
    if skill_file and skill_file.is_file():
        return skill_file.read_text(encoding="utf-8")
    return (
        "---\n"
        f"name: {skill_name}\n"
        "description: Connect AI coding agents to Bridge (https://bridge.xray.tech) via bridge / brg and bridge-mcp.\n"
        "---\n\n"
        f"# Bridge Agent Skill ({skill_name})\n\n"
        "Run `bridge context --days 7 --json` or call MCP tool `bridge_context_pull` at session start.\n"
    )


def _install_skill_bundle(skills_root: Path, files_written: list[str]) -> None:
    for skill_name in SKILL_BUNDLE_NAMES:
        dest = skills_root / skill_name / "SKILL.md"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(_package_skill_markdown(skill_name), encoding="utf-8")
        files_written.append(str(dest))


def _ensure_pretooluse_guard_hook(
    hooks_file: Path,
    guard_cmd: str,
    files_written: list[str],
    *,
    default_matcher: str = "Edit|Write|MultiEdit|Bash",
    antigravity_format: bool = False,
    statusbar_cmd: str | None = None,
) -> None:
    existing: dict[str, Any] = {}
    if hooks_file.is_file():
        try:
            loaded = json.loads(hooks_file.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                existing = loaded
        except Exception:
            existing = {}

    if antigravity_format:
        # Antigravity hooks.json uses named hook groups at the top level (e.g. "bridge-guard": {"PreToolUse": [...]}).
        # Deduplicate any top-level "hooks" entry that also calls brg/bridge guard.
        if isinstance(existing.get("hooks"), dict):
            hooks_pre = existing["hooks"].get("PreToolUse")
            if isinstance(hooks_pre, list):
                kept_pre = [
                    grp
                    for grp in hooks_pre
                    if not any(
                        "brg guard" in str(hk.get("command", ""))
                        or "bridge guard" in str(hk.get("command", ""))
                        for hk in (grp.get("hooks") or [])
                        if isinstance(hk, dict)
                    )
                ]
                if kept_pre:
                    existing["hooks"]["PreToolUse"] = kept_pre
                else:
                    existing["hooks"].pop("PreToolUse", None)
            if not existing["hooks"]:
                existing.pop("hooks", None)

        # Ensure every user gets a PreInvocation status-bar hook unless a custom PreInvocation hook is already registered.
        if statusbar_cmd:
            has_custom_preinv = False
            for grp_name, grp_val in existing.items():
                if grp_name == "bridge-status" or not isinstance(grp_val, dict):
                    continue
                pre_inv_list = grp_val.get("PreInvocation")
                if isinstance(pre_inv_list, list) and pre_inv_list:
                    has_custom_preinv = True
                    break
            if not has_custom_preinv:
                existing["bridge-status"] = {
                    "PreInvocation": [
                        {
                            "type": "command",
                            "command": f"{statusbar_cmd} --hook",
                            "timeout": 3,
                        }
                    ]
                }

        existing["bridge-guard"] = {
            "PreToolUse": [
                {
                    "matcher": default_matcher,
                    "hooks": [
                        {
                            "type": "command",
                            "command": guard_cmd,
                            "timeout": 10,
                        }
                    ],
                }
            ]
        }
        hooks_file.parent.mkdir(parents=True, exist_ok=True)
        hooks_file.write_text(json.dumps(existing, indent=2) + "\n", encoding="utf-8")
        files_written.append(str(hooks_file))
        return

    hooks_section = existing.setdefault("hooks", {})
    if not isinstance(hooks_section, dict):
        hooks_section = {}
        existing["hooks"] = hooks_section

    pre_list = hooks_section.get("PreToolUse")
    if not isinstance(pre_list, list):
        pre_list = []

    has_brg_guard = False
    for group in pre_list:
        if not isinstance(group, dict):
            continue
        inner_hooks = group.get("hooks")
        if not isinstance(inner_hooks, list):
            continue
        for hk in inner_hooks:
            if not isinstance(hk, dict):
                continue
            cmd_str = str(hk.get("command") or "")
            if "brg guard" in cmd_str or "bridge guard" in cmd_str:
                hk["command"] = guard_cmd
                has_brg_guard = True

    if not has_brg_guard:
        pre_list.append(
            {
                "matcher": default_matcher,
                "hooks": [{"type": "command", "command": guard_cmd}],
            }
        )
    hooks_section["PreToolUse"] = pre_list

    if statusbar_cmd:
        ups_list = hooks_section.get("UserPromptSubmit")
        if not isinstance(ups_list, list):
            ups_list = []
        has_sb_hook = any(
            isinstance(grp, dict)
            and any(
                isinstance(hk, dict) and "status-bar" in str(hk.get("command") or "")
                for hk in (grp.get("hooks") or [])
            )
            for grp in ups_list
        )
        if not has_sb_hook:
            ups_list.append(
                {
                    "matcher": ".*",
                    "hooks": [{"type": "command", "command": f"{statusbar_cmd} --text"}],
                }
            )
        hooks_section["UserPromptSubmit"] = ups_list

    hooks_file.parent.mkdir(parents=True, exist_ok=True)
    hooks_file.write_text(json.dumps(existing, indent=2) + "\n", encoding="utf-8")
    files_written.append(str(hooks_file))


def _setup_global_machine(
    home_dir: Path,
    files_written: list[str],
    *,
    target_filter: str = "all",
    api_key: str | None = None,
    api_url: str | None = None,
) -> None:
    """Configure global Claude Code, Antigravity, Cursor, and Codex harnesses on the machine."""
    brg_bin = home_dir / ".local" / "bin" / "brg"
    guard_cmd = f"{brg_bin} guard" if brg_bin.exists() else "brg guard"
    statusbar_cmd = f"{brg_bin} status-bar" if brg_bin.exists() else "brg status-bar"
    mcp_bin = home_dir / ".local" / "bin" / "bridge-mcp"
    mcp_cmd = str(mcp_bin) if mcp_bin.exists() else "bridge-mcp"

    if target_filter in ("all", "machine", "antigravity"):
        plugin_root = home_dir / ".gemini" / "config" / "plugins" / "bridge"
        plugin_root.mkdir(parents=True, exist_ok=True)

        plugin_src = _package_asset_path("plugin.json")
        if plugin_src and plugin_src.is_file():
            plugin_dest = plugin_root / "plugin.json"
            plugin_dest.write_text(plugin_src.read_text(encoding="utf-8"), encoding="utf-8")
            files_written.append(str(plugin_dest))

        hooks_src = _package_asset_path("hooks.json")
        if hooks_src and hooks_src.is_file():
            hooks_dest = plugin_root / "hooks" / "hooks.json"
            hooks_dest.parent.mkdir(parents=True, exist_ok=True)
            hooks_dest.write_text(hooks_src.read_text(encoding="utf-8"), encoding="utf-8")
            files_written.append(str(hooks_dest))

        _install_skill_bundle(plugin_root / "skills", files_written)
        _ensure_pretooluse_guard_hook(
            home_dir / ".gemini" / "config" / "hooks.json",
            guard_cmd,
            files_written,
            default_matcher="run_command|write_to_file|write_file|replace_file_content|multi_replace_file_content|edit_file|multi_edit|MultiEdit|NotebookEdit|mcp_filesystem_write_file|mcp_filesystem_edit_file|mcp_filesystem_move_file",
            antigravity_format=True,
            statusbar_cmd=statusbar_cmd,
        )

        agy_mcp_path = home_dir / ".gemini" / "antigravity" / "mcp_config.json"
        if agy_mcp_path.parent.is_dir() or agy_mcp_path.is_file():
            agy_data: dict[str, Any] = {}
            if agy_mcp_path.is_file():
                try:
                    loaded = json.loads(agy_mcp_path.read_text(encoding="utf-8"))
                    if isinstance(loaded, dict):
                        agy_data = loaded
                except Exception:
                    agy_data = {}
            servers = agy_data.setdefault("mcpServers", {})
            if isinstance(servers, dict):
                args_list: list[str] = []
                if api_url:
                    args_list.extend(["--url", api_url])
                servers["bridge_bridge-mcp"] = {"command": mcp_cmd, "args": args_list}
                agy_mcp_path.parent.mkdir(parents=True, exist_ok=True)
                agy_mcp_path.write_text(json.dumps(agy_data, indent=2) + "\n", encoding="utf-8")
                files_written.append(str(agy_mcp_path))

    if target_filter in ("all", "machine", "claude"):
        _install_skill_bundle(home_dir / ".claude" / "skills", files_written)
        _ensure_pretooluse_guard_hook(
            home_dir / ".claude" / "settings.json",
            guard_cmd,
            files_written,
            default_matcher="Edit|Write|MultiEdit|Bash",
            statusbar_cmd=statusbar_cmd,
        )

    if target_filter in ("all", "machine", "cursor"):
        _install_skill_bundle(home_dir / ".cursor" / "skills", files_written)
        cursor_mcp = home_dir / ".cursor" / "mcp.json"
        cursor_mcp.parent.mkdir(parents=True, exist_ok=True)
        existing_cursor: dict[str, Any] = {}
        if cursor_mcp.is_file():
            try:
                loaded = json.loads(cursor_mcp.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    existing_cursor = loaded
            except Exception:
                existing_cursor = {}
        existing_cursor.setdefault("mcpServers", {})["bridge"] = {"command": mcp_cmd, "args": []}
        cursor_mcp.write_text(json.dumps(existing_cursor, indent=2) + "\n", encoding="utf-8")
        files_written.append(str(cursor_mcp))

    if target_filter in ("all", "machine", "codex"):
        _install_skill_bundle(home_dir / ".agents" / "skills", files_written)
        codex_hooks = home_dir / ".codex" / "hooks.json"
        if codex_hooks.parent.is_dir() or codex_hooks.is_file():
            _ensure_pretooluse_guard_hook(
                codex_hooks,
                guard_cmd,
                files_written,
                default_matcher="Bash|Edit|Write",
            )


def handle_setup(
    args: Any,
    config: BridgeConfig,
    *,
    force_json: bool = False,
    stdout_stream: Any = None,
) -> int:
    """Prepare machine or workspace for Bridge (MCP server, SKILL.md bundle, and safety hooks)."""
    target = str(getattr(args, "target", "all") or "all").strip().lower()
    global_scope = bool(getattr(args, "global_scope", False)) or target in ("all", "machine")

    root = config.workspace_root
    home_override = config.env.get("HOME")
    home_dir = Path(home_override) if home_override else Path.home()

    files_written: list[str] = []

    mcp_entry = {
        "command": "bridge-mcp",
        "args": [],
    }

    if target in ("all", "machine"):
        _setup_global_machine(
            home_dir,
            files_written,
            target_filter="all",
            api_key=config.api_key,
            api_url=config.api_url,
        )
    elif target == "claude":
        if global_scope:
            _setup_global_machine(
                home_dir,
                files_written,
                target_filter="claude",
                api_key=config.api_key,
                api_url=config.api_url,
            )
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

        _install_skill_bundle(root / ".claude" / "skills", files_written)

    elif target == "cursor":
        if global_scope:
            _setup_global_machine(
                home_dir,
                files_written,
                target_filter="cursor",
                api_key=config.api_key,
                api_url=config.api_url,
            )
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
        _install_skill_bundle(root / ".cursor" / "skills", files_written)

    elif target == "antigravity":
        if global_scope:
            _setup_global_machine(
                home_dir,
                files_written,
                target_filter="antigravity",
                api_key=config.api_key,
                api_url=config.api_url,
            )
        _install_skill_bundle(root / ".agent" / "skills", files_written)

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
        if global_scope:
            _setup_global_machine(
                home_dir,
                files_written,
                target_filter="codex",
                api_key=config.api_key,
                api_url=config.api_url,
            )
        _install_skill_bundle(root / ".agents" / "skills", files_written)

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
            'b=subprocess.run(["git","rev-parse","--abbrev-ref","HEAD"],capture_output=True,text=True,errors="replace").stdout.strip(); '
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
            "# Bridge post-commit hook: prints live WORK/LOCAL status-bar and logs commit signal ONLY in bound WORK repos\n"
            "if command -v brg >/dev/null 2>&1; then\n"
            '  STATUS_BAR=$(brg status-bar --text 2>/dev/null)\n'
            '  if [ -n "$STATUS_BAR" ]; then\n'
            '    printf "%s\\n" "$STATUS_BAR"\n'
            "  fi\n"
            '  if [ -f ".bridge.json" ] || [ -f ".workspace-project.json" ]; then\n'
            '    MSG=$(git log -1 --pretty=%s 2>/dev/null)\n'
            '    SHA=$(git rev-parse HEAD 2>/dev/null)\n'
            '    brg seam win "$MSG" --commit "$SHA" >/dev/null 2>&1 || true\n'
            "  fi\n"
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
                "message": f"Unsupported setup target '{target}'. Choose from: all, machine, claude, cursor, antigravity, codex, git-hooks.",
            },
            f"Error: Unsupported setup target '{target}'.",
            force_json=force_json,
            stdout_stream=stdout_stream,
        )
        return EXIT_ERROR

    payload = {
        "ok": True,
        "target": target,
        "globalScope": global_scope,
        "filesWritten": files_written,
    }
    human_lines = [
        f"Configured Bridge integration for '{target}' (global={global_scope}):",
        f"  Files configured ({len(files_written)}):",
        *[f"    - {fp}" for fp in files_written[:15]],
        *(
            [f"    ... and {len(files_written) - 15} more files"]
            if len(files_written) > 15
            else []
        ),
    ]
    emit_output(
        payload,
        human_lines,
        force_json=force_json,
        stdout_stream=stdout_stream,
    )
    return EXIT_OK
