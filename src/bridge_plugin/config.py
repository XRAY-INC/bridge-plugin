"""Configuration, credentials, branch-to-task state (.bridge/state.json), and local claims."""

from __future__ import annotations

import base64
import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from .runtime import (
    DEFAULT_API_URL,
    detect_git_branch,
    detect_git_repo_slug,
    detect_git_root,
)

PROJECT_CONFIG_FILENAMES = (
    ".bridge.json",
    ".workspace-project.json",
    ".xray-dispatch.json",
)

SUPPORTED_API_KEY_ENVS = (
    "BRIDGE_API_KEY",
    "WORKSPACE_API_KEY",
    "XRAY_DISPATCH_API_KEY",
)


def decode_api_key_claims(api_key: str | None) -> dict[str, Any]:
    """Reads the workspace a key was issued for. Only the server can say whether the key is valid."""
    parts = (api_key or "").strip().split(".")
    if len(parts) < 2 or parts[0] != "xrkey_v1":
        return {}
    try:
        padded = parts[1] + "=" * (-len(parts[1]) % 4)
        claims = json.loads(base64.urlsafe_b64decode(padded))
    except (ValueError, TypeError):
        return {}
    return claims if isinstance(claims, dict) else {}


@dataclass
class BridgeConfig:
    """Resolved Bridge configuration for the active workspace and user."""

    organization_id: str | None = None
    api_url: str = DEFAULT_API_URL
    repository: str | None = None
    api_key: str | None = None
    user_email: str | None = None
    agency_organization_id: str | None = None
    agency_api_key: str | None = None
    is_internal_target: bool = False
    config_path: Path | None = None
    credentials_path: Path = field(default_factory=lambda: Path.home() / ".config" / "bridge" / "credentials.json")
    workspace_root: Path = field(default_factory=Path.cwd)
    auth_source: str = "none"
    env: Mapping[str, str] = field(default_factory=lambda: os.environ)

    def for_agency(self) -> BridgeConfig:
        """Return a BridgeConfig scoped to the upstream Agency / Home workspace (e.g. org_xray_tech)."""
        target_org = self.agency_organization_id or self.organization_id
        target_key = self.agency_api_key or self.api_key
        return BridgeConfig(
            organization_id=target_org,
            api_url=self.api_url,
            repository=self.repository,
            api_key=target_key,
            user_email=self.user_email,
            agency_organization_id=self.agency_organization_id,
            agency_api_key=self.agency_api_key,
            is_internal_target=True,
            config_path=self.config_path,
            credentials_path=self.credentials_path,
            workspace_root=self.workspace_root,
            auth_source=self.auth_source,
            env=self.env,
        )

    @property
    def is_authenticated(self) -> bool:
        return bool(self.api_key and self.api_key.strip())

    @property
    def is_bound(self) -> bool:
        return bool(self.organization_id and self.organization_id.strip())

    @property
    def key_hint(self) -> str | None:
        if not self.api_key:
            return None
        k = self.api_key.strip()
        if len(k) <= 16:
            return f"{k[:4]}..."
        return f"{k[:14]}...{k[-4:]}"

    @property
    def key_label(self) -> str | None:
        label = decode_api_key_claims(self.api_key).get("label")
        if isinstance(label, str) and label.strip() and label.strip() != "xray-dispatch":
            return label.strip()
        return None

    @property
    def bridge_dir(self) -> Path:
        return self.workspace_root / ".bridge"

    @property
    def state_path(self) -> Path:
        return self.bridge_dir / "state.json"

    @property
    def spool_path(self) -> Path:
        return self.bridge_dir / "spool.jsonl"

    @property
    def claims_path(self) -> Path:
        return self.bridge_dir / "claims.json"


def get_credentials_path(env: Mapping[str, str] | None = None) -> Path:
    """Return path to ~/.config/bridge/credentials.json (or BRIDGE_CREDENTIALS_PATH override)."""
    environ = env if env is not None else os.environ
    override = environ.get("BRIDGE_CREDENTIALS_PATH", "").strip()
    if override:
        return Path(override)
    xdg = environ.get("XDG_CONFIG_HOME", "").strip()
    base = Path(xdg) if xdg else (Path.home() / ".config")
    return base / "bridge" / "credentials.json"


def load_user_credentials(env: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Load credentials from ~/.config/bridge/credentials.json."""
    cred_path = get_credentials_path(env=env)
    if not cred_path.is_file():
        return {}
    try:
        return json.loads(cred_path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_user_credentials(
    *,
    api_key: str,
    api_url: str = DEFAULT_API_URL,
    organization_id: str | None = None,
    user_email: str | None = None,
    agency_organization_id: str | None = None,
    env: Mapping[str, str] | None = None,
) -> Path:
    """Save user credentials to ~/.config/bridge/credentials.json with 0600 permissions and multi-workspace keyring."""
    cred_path = get_credentials_path(env=env)
    cred_path.parent.mkdir(parents=True, exist_ok=True)
    existing = load_user_credentials(env=env)
    workspaces: dict[str, Any] = (
        dict(existing["workspaces"])
        if isinstance(existing.get("workspaces"), dict)
        else {}
    )

    cleaned_key = api_key.strip()
    cleaned_url = (api_url or DEFAULT_API_URL).rstrip("/")
    resolved_org = (
        (organization_id or "").strip()
        or str(decode_api_key_claims(cleaned_key).get("organizationId") or "").strip()
        or None
    )
    now_iso = datetime.now(timezone.utc).isoformat()

    if resolved_org:
        ws_entry: dict[str, Any] = {
            "apiKey": cleaned_key,
            "apiUrl": cleaned_url,
            "organizationId": resolved_org,
            "updatedAt": now_iso,
        }
        if user_email:
            ws_entry["userEmail"] = user_email.strip()
        workspaces[resolved_org] = ws_entry

    payload: dict[str, Any] = {
        **existing,
        "apiKey": cleaned_key,
        "apiUrl": cleaned_url,
        "updatedAt": now_iso,
        "workspaces": workspaces,
    }
    if resolved_org:
        payload["organizationId"] = resolved_org
    if user_email:
        payload["userEmail"] = user_email.strip()
    if agency_organization_id:
        payload["agencyOrganizationId"] = agency_organization_id.strip()

    cred_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    try:
        os.chmod(cred_path, 0o600)
    except Exception:
        pass
    return cred_path


def clear_user_credentials(env: Mapping[str, str] | None = None) -> bool:
    """Delete ~/.config/bridge/credentials.json if present."""
    cred_path = get_credentials_path(env=env)
    if cred_path.is_file():
        cred_path.unlink()
        return True
    return False


def find_project_config_file(start_dir: Path | None = None) -> Path | None:
    """Walk up from start_dir to locate .bridge.json, .workspace-project.json, or .xray-dispatch.json."""
    current = (start_dir or Path.cwd()).resolve()
    for directory in (current, *current.parents):
        for filename in PROJECT_CONFIG_FILENAMES:
            candidate = directory / filename
            if candidate.is_file():
                return candidate
        if (directory / ".git").exists():
            break
    return None


def resolve_config(
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
    *,
    explicit_org: str | None = None,
    explicit_agency_org: str | None = None,
    explicit_url: str | None = None,
    explicit_repo: str | None = None,
    explicit_api_key: str | None = None,
) -> BridgeConfig:
    """Resolve Bridge configuration across CLI flags, env vars, .bridge.json, and user credentials."""
    environ = env if env is not None else os.environ
    work_dir = (cwd or Path.cwd()).resolve()
    git_root = detect_git_root(cwd=work_dir)
    workspace_root = git_root or work_dir

    cfg_file = find_project_config_file(start_dir=work_dir)
    file_data: dict[str, Any] = {}
    if cfg_file and cfg_file.is_file():
        workspace_root = cfg_file.parent
        try:
            file_data = json.loads(cfg_file.read_text(encoding="utf-8"))
        except Exception:
            file_data = {}

    creds = load_user_credentials(env=environ)
    cred_path = get_credentials_path(env=environ)
    workspaces_keyring = creds.get("workspaces") if isinstance(creds.get("workspaces"), dict) else {}

    # 1. Resolve Organization ID & Agency Organization ID
    org_id = (
        explicit_org
        or environ.get("BRIDGE_ORG_ID")
        or environ.get("WORKSPACE_ORG_ID")
        or file_data.get("organizationId")
        or file_data.get("orgId")
        or creds.get("organizationId")
        or None
    )
    if isinstance(org_id, str):
        org_id = org_id.strip() or None

    agency_org_id = (
        explicit_agency_org
        or environ.get("BRIDGE_AGENCY_ORG_ID")
        or file_data.get("agencyOrganizationId")
        or file_data.get("upstreamOrganizationId")
        or creds.get("agencyOrganizationId")
        or None
    )
    if isinstance(agency_org_id, str):
        agency_org_id = agency_org_id.strip() or None
    if agency_org_id and agency_org_id == org_id:
        agency_org_id = None

    # 2. Resolve API Key & Auth Source (checking workspace keyring for matching org_id)
    api_key: str | None = None
    auth_source = "none"

    if explicit_api_key and explicit_api_key.strip():
        api_key = explicit_api_key.strip()
        auth_source = "flag"
    else:
        for env_name in SUPPORTED_API_KEY_ENVS:
            val = environ.get(env_name, "").strip()
            if val:
                api_key = val
                auth_source = f"env:{env_name}"
                break
        if not api_key and isinstance(file_data.get("apiKey"), str) and file_data["apiKey"].strip():
            api_key = file_data["apiKey"].strip()
            auth_source = f"file:{cfg_file.name if cfg_file else '.bridge.json'}"
        if (
            not api_key
            and org_id
            and isinstance(workspaces_keyring.get(org_id), dict)
            and isinstance(workspaces_keyring[org_id].get("apiKey"), str)
            and workspaces_keyring[org_id]["apiKey"].strip()
        ):
            api_key = workspaces_keyring[org_id]["apiKey"].strip()
            auth_source = f"credentials:{org_id}"
        if not api_key and isinstance(creds.get("apiKey"), str) and creds["apiKey"].strip():
            api_key = creds["apiKey"].strip()
            auth_source = "credentials"

    # 3. Resolve Agency API Key (for dual-homed Client + Agency workspaces)
    agency_api_key: str | None = (
        environ.get("BRIDGE_AGENCY_API_KEY", "").strip()
        or (file_data.get("agencyApiKey", "").strip() if isinstance(file_data.get("agencyApiKey"), str) else "")
        or None
    )
    if (
        not agency_api_key
        and agency_org_id
        and isinstance(workspaces_keyring.get(agency_org_id), dict)
        and isinstance(workspaces_keyring[agency_org_id].get("apiKey"), str)
    ):
        agency_api_key = workspaces_keyring[agency_org_id]["apiKey"].strip() or None
    if not agency_api_key:
        agency_api_key = api_key

    # 4. Resolve API URL
    api_url = (
        explicit_url
        or environ.get("BRIDGE_API_URL")
        or environ.get("WORKSPACE_API_URL")
        or file_data.get("apiUrl")
        or creds.get("apiUrl")
        or DEFAULT_API_URL
    )
    api_url = str(api_url).strip().rstrip("/") or DEFAULT_API_URL

    # 5. Resolve Repository Slug
    repo = (
        explicit_repo
        or environ.get("BRIDGE_REPO")
        or file_data.get("repository")
        or file_data.get("repo")
        or detect_git_repo_slug(cwd=workspace_root, env=environ)
    )

    user_email = (
        environ.get("BRIDGE_USER_EMAIL")
        or file_data.get("userEmail")
        or creds.get("userEmail")
        or None
    )

    return BridgeConfig(
        organization_id=org_id,
        api_url=api_url,
        repository=repo,
        api_key=api_key,
        user_email=user_email,
        agency_organization_id=agency_org_id,
        agency_api_key=agency_api_key,
        config_path=cfg_file,
        credentials_path=cred_path,
        workspace_root=workspace_root,
        auth_source=auth_source,
        env=environ,
    )


def write_project_config(
    target_dir: Path,
    *,
    organization_id: str,
    repository: str,
    api_url: str = DEFAULT_API_URL,
    agency_organization_id: str | None = None,
    filename: str = ".bridge.json",
) -> Path:
    """Write .bridge.json in the workspace root."""
    target_dir.mkdir(parents=True, exist_ok=True)
    path = target_dir / filename
    payload: dict[str, Any] = {
        "plugin": "bridge",
        "organizationId": organization_id.strip(),
        "apiUrl": api_url.strip().rstrip("/"),
        "repository": repository.strip(),
        "apiKeyEnv": "BRIDGE_API_KEY",
        "fallbackApiKeyEnv": "WORKSPACE_API_KEY",
        "supportedEnvVars": list(SUPPORTED_API_KEY_ENVS),
    }
    if agency_organization_id and agency_organization_id.strip():
        payload["agencyOrganizationId"] = agency_organization_id.strip()
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Branch-to-Task State (.bridge/state.json)
# ---------------------------------------------------------------------------


def load_branch_state_map(config: BridgeConfig) -> dict[str, Any]:
    """Load .bridge/state.json containing branch -> { taskId, jobId, title, ... }."""
    if not config.state_path.is_file():
        return {"branches": {}, "lastSeamId": None}
    try:
        data = json.loads(config.state_path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {"branches": {}, "lastSeamId": None}
        data.setdefault("branches", {})
        return data
    except Exception:
        return {"branches": {}, "lastSeamId": None}


def save_branch_state_map(config: BridgeConfig, state_data: dict[str, Any]) -> Path:
    """Persist .bridge/state.json."""
    config.bridge_dir.mkdir(parents=True, exist_ok=True)
    config.state_path.write_text(json.dumps(state_data, indent=2) + "\n", encoding="utf-8")
    return config.state_path


def bind_branch_state(
    config: BridgeConfig,
    branch: str,
    *,
    task_id: str | None = None,
    job_id: str | None = None,
    title: str | None = None,
    worktree_path: str | None = None,
) -> dict[str, Any]:
    """Bind a Git branch to a taskId and/or jobId inside .bridge/state.json."""
    state = load_branch_state_map(config)
    branches = state.setdefault("branches", {})
    existing = branches.get(branch, {}) if isinstance(branches.get(branch), dict) else {}
    entry = {
        "branch": branch,
        "taskId": task_id if task_id is not None else existing.get("taskId"),
        "jobId": job_id if job_id is not None else existing.get("jobId"),
        "title": title if title is not None else existing.get("title"),
        "worktreePath": worktree_path if worktree_path is not None else existing.get("worktreePath"),
        "boundAt": datetime.now(timezone.utc).isoformat(),
    }
    branches[branch] = entry
    state["activeBranch"] = branch
    save_branch_state_map(config, state)
    return entry


def get_branch_state(config: BridgeConfig, branch: str | None = None) -> dict[str, Any]:
    """Retrieve the bound { taskId, jobId, title } for the specified or current Git branch."""
    resolved_branch = branch or detect_git_branch(cwd=config.workspace_root, env=config.env)
    state = load_branch_state_map(config)
    branches = state.get("branches", {})
    if isinstance(branches, dict) and isinstance(branches.get(resolved_branch), dict):
        return dict(branches[resolved_branch])
    return {"branch": resolved_branch, "taskId": None, "jobId": None, "title": None}


def record_last_seam_id(config: BridgeConfig, seam_id: str) -> None:
    """Store the most recently created seam ID in .bridge/state.json for 'bridge seam edit/retract last'."""
    state = load_branch_state_map(config)
    state["lastSeamId"] = seam_id
    save_branch_state_map(config, state)


def get_last_seam_id(config: BridgeConfig) -> str | None:
    """Return the most recently created seam ID from .bridge/state.json."""
    state = load_branch_state_map(config)
    val = state.get("lastSeamId")
    return str(val) if val else None


# ---------------------------------------------------------------------------
# Local Scope Claims Cache (.bridge/claims.json)
# ---------------------------------------------------------------------------


def load_local_claims(config: BridgeConfig) -> list[dict[str, Any]]:
    """Return non-expired local scope claims from .bridge/claims.json."""
    if not config.claims_path.is_file():
        return []
    try:
        raw = json.loads(config.claims_path.read_text(encoding="utf-8"))
        claims = raw.get("claims", []) if isinstance(raw, dict) else []
        now = time.time()
        active: list[dict[str, Any]] = []
        for item in claims:
            if not isinstance(item, dict):
                continue
            if item.get("releasedAt"):
                continue
            exp_epoch = item.get("expiresAtEpoch")
            if isinstance(exp_epoch, (int, float)) and exp_epoch <= now:
                continue
            active.append(item)
        return active
    except Exception:
        return []


def save_local_claims(config: BridgeConfig, claims: list[dict[str, Any]]) -> Path:
    """Write active local claims to .bridge/claims.json."""
    config.bridge_dir.mkdir(parents=True, exist_ok=True)
    config.claims_path.write_text(
        json.dumps({"claims": claims}, indent=2) + "\n",
        encoding="utf-8",
    )
    return config.claims_path
