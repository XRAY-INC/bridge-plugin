"""Modular, information-dense Turn Status Bar (`brg status-bar`) with Work vs. Local scope transparency."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any, Mapping

from ..config import (
    BridgeConfig,
    get_branch_state,
    get_credentials_path,
    load_local_claims,
    load_user_credentials,
    resolve_config,
)
from ..runtime import (
    EXIT_OK,
    detect_git_branch,
    detect_git_repo_slug,
    detect_runtime_context,
    emit_output,
)
from .safety import load_frozen_globs, load_snapshots

ALL_STATUS_SEGMENTS: tuple[str, ...] = (
    "scope",
    "repo",
    "git",
    "user",
    "agent",
    "model",
    "task",
    "leases",
    "savepoints",
    "frozen",
    "spool",
)


def _detect_git_dirty(cwd: Path) -> bool:
    """Return True if the git working tree at cwd has uncommitted tracked changes."""
    if not cwd.exists():
        return False
    try:
        proc = subprocess.run(
            ["git", "status", "--porcelain", "-uno"],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=1.5,
            check=False,
        )
        return proc.returncode == 0 and bool(proc.stdout.strip())
    except Exception:
        return False


def _detect_git_user_identity(cwd: Path) -> tuple[str | None, str | None]:
    """Return (git user.name, git user.email) from local/global git config."""
    if not cwd.exists():
        return (None, None)
    name: str | None = None
    email: str | None = None
    try:
        proc_n = subprocess.run(
            ["git", "config", "--get", "user.name"],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=1.5,
            check=False,
        )
        if proc_n.returncode == 0 and proc_n.stdout.strip():
            name = proc_n.stdout.strip()
        proc_e = subprocess.run(
            ["git", "config", "--get", "user.email"],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=1.5,
            check=False,
        )
        if proc_e.returncode == 0 and proc_e.stdout.strip():
            email = proc_e.stdout.strip()
    except Exception:
        pass
    return (name, email)


def _detect_harness_from_process_tree(
    env: Mapping[str, str] | None = None,
    max_depth: int = 6,
) -> str | None:
    """Walk parent processes to detect Antigravity, Claude Code, Cursor, or Codex when env vars are absent."""
    environ = env if env is not None else os.environ
    if environ.get("PYTEST_CURRENT_TEST") and not environ.get("BRIDGE_TEST_PROCESS_WALK"):
        return None
    pid = os.getppid()
    for _ in range(max_depth):
        if pid <= 1:
            break
        try:
            proc = subprocess.run(
                ["ps", "-o", "ppid=,comm=,args=", "-p", str(pid)],
                capture_output=True,
                text=True,
                timeout=0.3,
                check=False,
            )
            if proc.returncode != 0 or not proc.stdout.strip():
                break
            line = proc.stdout.strip().splitlines()[0].strip()
            parts = line.split(None, 1)
            next_pid = int(parts[0]) if parts and parts[0].isdigit() else 0
            cmd_text = (parts[1] if len(parts) > 1 else "").lower()
            if "antigravity" in cmd_text or "language_server_macos" in cmd_text:
                return "antigravity"
            if re.search(r"(?:^|/|\s)claude(?:\s|$)", cmd_text):
                return "claude"
            if re.search(r"(?:^|/|\s)cursor(?:\s|$)", cmd_text):
                return "cursor"
            if re.search(r"(?:^|/|\s)codex(?:\s|$)", cmd_text):
                return "codex"
            pid = next_pid
        except Exception:
            break
    return None


def _detect_claude_session_model(
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> str | None:
    """Inspect Claude Code's session JSONL transcript or settings.json to detect the active model."""
    environ = env if env is not None else os.environ
    if environ.get("PYTEST_CURRENT_TEST") and not environ.get("CLAUDE_CONFIG_DIR"):
        return None
    try:
        claude_home = Path(
            environ.get("CLAUDE_CONFIG_DIR", "").strip()
            or str(Path.home() / ".claude")
        )
        projects_root = claude_home / "projects"
        if cwd and projects_root.is_dir():
            slug = re.sub(r"[^a-zA-Z0-9-]", "-", str(cwd.resolve()))
            proj_dir = projects_root / slug
            if proj_dir.is_dir():
                jsonls = sorted(
                    (p for p in proj_dir.glob("*.jsonl") if p.is_file()),
                    key=lambda p: p.stat().st_mtime,
                    reverse=True,
                )
                if jsonls:
                    jpath = jsonls[0]
                    size = jpath.stat().st_size
                    with jpath.open("rb") as fh:
                        if size > 32768:
                            fh.seek(size - 32768)
                        tail_text = fh.read().decode("utf-8", errors="ignore")
                    for line in reversed(tail_text.splitlines()):
                        line = line.strip()
                        if not line or '"model"' not in line:
                            continue
                        try:
                            row = json.loads(line)
                        except Exception:
                            continue
                        if not isinstance(row, dict):
                            continue
                        msg = row.get("message") if isinstance(row.get("message"), dict) else row
                        mdl = msg.get("model") if isinstance(msg, dict) else None
                        if isinstance(mdl, str) and mdl.strip() and not mdl.strip().startswith("<"):
                            return mdl.strip()
        settings_path = claude_home / "settings.json"
        if settings_path.is_file():
            raw = json.loads(settings_path.read_text(encoding="utf-8"))
            if isinstance(raw, dict) and isinstance(raw.get("model"), str) and raw["model"].strip():
                return raw["model"].strip()
    except Exception:
        pass
    return None


def is_work_context(config: BridgeConfig) -> bool:
    """Return True when the current directory/repo is explicitly bound to a Bridge workspace.

    A directory is in WORK context when:
    1. A project binding file (`.bridge.json` or `.workspace-project.json`) exists in the directory hierarchy.
    2. Or `BRIDGE_ORG_ID`, `WORKSPACE_ORG_ID`, `BRIDGE_PROJECT`, or `BRIDGE_CLIENT` is explicitly set in the environment or CLI flags.

    Personal or unbound local directories (where `organizationId` only exists in global `~/.config/bridge/credentials.json`)
    return False (`LOCAL`), ensuring full transparency that the folder is not bound to a work workspace.
    """
    if config.config_path is not None and config.config_path.is_file():
        return True
    if config.project_name or config.client_name:
        return True
    environ = config.env
    for key in ("BRIDGE_ORG_ID", "WORKSPACE_ORG_ID", "BRIDGE_PROJECT", "BRIDGE_CLIENT"):
        if environ.get(key, "").strip():
            return True
    return False


def get_status_bar_config(config: BridgeConfig) -> dict[str, Any]:
    """Load statusBar configuration from credentials.json, .bridge.json, and environment variables."""
    segments: dict[str, bool] = {seg: True for seg in ALL_STATUS_SEGMENTS}
    enabled = True
    default_model: str | None = None

    creds = load_user_credentials(env=config.env)
    sb_creds = creds.get("statusBar")
    if isinstance(sb_creds, bool):
        enabled = sb_creds
    elif isinstance(sb_creds, dict):
        if "enabled" in sb_creds:
            enabled = bool(sb_creds["enabled"])
        if isinstance(sb_creds.get("model"), str) and sb_creds["model"].strip():
            default_model = sb_creds["model"].strip()
        segs_dict = sb_creds.get("segments") if isinstance(sb_creds.get("segments"), dict) else sb_creds
        for seg in ALL_STATUS_SEGMENTS:
            if seg in segs_dict and isinstance(segs_dict[seg], bool):
                segments[seg] = segs_dict[seg]

    if config.config_path and config.config_path.is_file():
        try:
            proj_data = json.loads(config.config_path.read_text(encoding="utf-8"))
            sb_proj = proj_data.get("statusBar") if isinstance(proj_data, dict) else None
            if isinstance(sb_proj, bool):
                enabled = sb_proj
            elif isinstance(sb_proj, dict):
                if "enabled" in sb_proj:
                    enabled = bool(sb_proj["enabled"])
                if isinstance(sb_proj.get("model"), str) and sb_proj["model"].strip():
                    default_model = sb_proj["model"].strip()
                segs_dict = sb_proj.get("segments") if isinstance(sb_proj.get("segments"), dict) else sb_proj
                for seg in ALL_STATUS_SEGMENTS:
                    if seg in segs_dict and isinstance(segs_dict[seg], bool):
                        segments[seg] = segs_dict[seg]
        except Exception:
            pass

    env_segs = config.env.get("BRIDGE_STATUS_SEGMENTS", "").strip()
    if env_segs:
        wanted = {s.strip().lower() for s in env_segs.split(",") if s.strip()}
        for seg in ALL_STATUS_SEGMENTS:
            segments[seg] = seg in wanted

    env_val = config.env.get("BRIDGE_STATUS_BAR", "").strip().lower()
    if env_val in ("0", "false", "off", "no", "disabled"):
        enabled = False
    elif env_val in ("1", "true", "on", "yes", "enabled"):
        enabled = True

    return {
        "enabled": enabled,
        "model": default_model,
        "segments": segments,
    }


def save_status_bar_config(
    config: BridgeConfig,
    *,
    enabled: bool | None = None,
    model: str | None = None,
    show_segments: list[str] | None = None,
    hide_segments: list[str] | None = None,
    only_segments: list[str] | None = None,
    reset_segments: bool = False,
) -> dict[str, Any]:
    """Persist statusBar preferences into ~/.config/bridge/credentials.json."""
    current = get_status_bar_config(config)
    eff_enabled = current["enabled"] if enabled is None else bool(enabled)
    eff_model = model if isinstance(model, str) and model.strip() else current.get("model")
    segs: dict[str, bool] = dict(current.get("segments") or {s: True for s in ALL_STATUS_SEGMENTS})

    if reset_segments:
        segs = {s: True for s in ALL_STATUS_SEGMENTS}
    if only_segments:
        wanted = {s.strip().lower() for s in only_segments if s.strip()}
        segs = {s: (s in wanted) for s in ALL_STATUS_SEGMENTS}
    if show_segments:
        for s in show_segments:
            key = s.strip().lower()
            if key in segs:
                segs[key] = True
    if hide_segments:
        for s in hide_segments:
            key = s.strip().lower()
            if key in segs:
                segs[key] = False

    env_override = (
        {"BRIDGE_CREDENTIALS_PATH": str(config.credentials_path)}
        if config.credentials_path
        else None
    )
    cred_path = config.credentials_path or get_credentials_path(env=config.env)
    cred_path.parent.mkdir(parents=True, exist_ok=True)
    existing = load_user_credentials(env=env_override or config.env)
    sb_block: dict[str, Any] = {
        "enabled": eff_enabled,
        "segments": segs,
    }
    if eff_model:
        sb_block["model"] = eff_model
    existing["statusBar"] = sb_block
    cred_path.write_text(json.dumps(existing, indent=2) + "\n", encoding="utf-8")
    try:
        os.chmod(cred_path, 0o600)
    except Exception:
        pass
    return {
        "ok": True,
        "path": str(cred_path),
        "enabled": eff_enabled,
        "model": eff_model,
        "segments": segs,
    }


def render_status_bar(
    config: BridgeConfig,
    *,
    explicit_agent: str | None = None,
    explicit_model: str | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Render the modular Bridge turn status bar and return both structured telemetry and the formatted line."""
    sb_cfg = get_status_bar_config(config)
    segs = sb_cfg.get("segments", {s: True for s in ALL_STATUS_SEGMENTS})
    enabled = bool(sb_cfg.get("enabled", True))

    root = config.workspace_root
    runtime = detect_runtime_context(cwd=root, env=config.env, explicit_agent=explicit_agent)
    work_bound = is_work_context(config)
    has_token = config.is_authenticated

    # 1. Scope Badge (WORK vs LOCAL transparency)
    if work_bound:
        scope_target = (
            config.client_name
            or config.project_name
            or config.organization_id
            or "bound"
        )
        if config.is_internal_target and config.agency_organization_id:
            scope_target = f"{config.agency_organization_id} (internal)"
        mode_badge = f"WORK:{scope_target}" if has_token else f"WORK:{scope_target}:local"
    else:
        mode_badge = "LOCAL"

    # 2. Repo + Git Branch + Dirty indicator
    repo_str = (
        config.repository
        or config.project_name
        or detect_git_repo_slug(cwd=root, env=config.env)
        or root.name
    )
    git_branch = detect_git_branch(cwd=root, env=config.env) if (root / ".git").exists() else None
    git_dirty = _detect_git_dirty(root) if git_branch else False

    # 3. User Identity (work identity in WORK context; git config fallback in LOCAL personal repos)
    git_name, git_email = _detect_git_user_identity(root)
    if work_bound:
        user_email = config.user_email or git_email
        user_name = config.user_name or config.user_alias or git_name
        user_source = config.user_source if config.user_source != "none" else ("git" if git_email else "default")
    else:
        # In unbound LOCAL personal folders, prefer explicit/session/project override first, then repo git user,
        # before falling back to global work credentials so personal repos don't masquerade as work repos.
        if config.user_source in ("flag", "env", "session", "project") and (config.user_email or config.user_alias):
            user_email = config.user_email
            user_name = config.user_name or config.user_alias
            user_source = config.user_source
        elif git_email or git_name:
            user_email = git_email
            user_name = git_name
            user_source = "git"
        else:
            user_email = config.user_email
            user_name = config.user_name or config.user_alias
            user_source = config.user_source

    if user_name and user_email and user_name.lower() != user_email.lower():
        user_display = f"{user_name} ({user_email})"
    elif user_email:
        user_display = user_email
    elif user_name:
        user_display = user_name
    else:
        user_display = config.env.get("USER") or config.env.get("USERNAME") or "developer"

    # 4. Agent / Harness & Model
    tree_agent = _detect_harness_from_process_tree(env=config.env)
    env_harness: str | None = None
    if config.env.get("ANTIGRAVITY_CLI") or config.env.get("ANTIGRAVITY_AGENT"):
        env_harness = "antigravity"
    elif config.env.get("CLAUDECODE") == "1" or config.env.get("CLAUDE_CODE_ENTRYPOINT"):
        env_harness = "claude"
    elif config.env.get("CURSOR_TRACE_ID") or config.env.get("CURSOR_SESSION_ID"):
        env_harness = "cursor"
    elif config.env.get("CODEX_SANDBOX") or config.env.get("OPENAI_CODEX"):
        env_harness = "codex"

    harness_name = explicit_agent or env_harness or tree_agent
    key_label = runtime.get("keyLabel")
    if explicit_agent:
        agent_str = explicit_agent
    elif harness_name and key_label and harness_name.lower() != key_label.lower():
        agent_str = f"{harness_name}:{key_label}" if not work_bound else harness_name
    elif harness_name:
        agent_str = harness_name
    else:
        agent_str = runtime["agentId"]

    model_str = (
        explicit_model
        or runtime.get("modelName")
        or sb_cfg.get("model")
        or None
    )
    if not model_str:
        lowered_agent = (harness_name or agent_str).lower()
        if "claude" in lowered_agent:
            model_str = _detect_claude_session_model(cwd=root, env=config.env) or "claude-opus-4-6"
        elif "antigravity" in lowered_agent or "gemini" in lowered_agent:
            model_str = "gemini"
        elif "cursor" in lowered_agent:
            model_str = "cursor-default"
        elif "codex" in lowered_agent:
            model_str = "codex"

    # 5. Bound Task, Active Leases, Savepoints, Frozen Files, Offline Spool
    branch_state = get_branch_state(config, branch=git_branch or "main")
    bound_task_id = branch_state.get("taskId")
    active_claims = load_local_claims(config)
    snapshots = load_snapshots(config)
    latest_savepoint = str(snapshots[0].get("id"))[:12] if snapshots else None
    frozen_globs = load_frozen_globs(config)

    spool_count = 0
    if config.spool_path.is_file():
        try:
            spool_count = sum(
                1 for ln in config.spool_path.read_text(encoding="utf-8").splitlines() if ln.strip()
            )
        except Exception:
            spool_count = 0

    parts: list[str] = ["BRIDGE"]
    if segs.get("scope", True):
        parts.append(mode_badge)

    if segs.get("repo", True) and segs.get("git", True) and git_branch:
        parts.append(f"{repo_str}:{git_branch}{'*' if git_dirty else ''}")
    elif segs.get("repo", True):
        parts.append(repo_str)
    elif segs.get("git", True) and git_branch:
        parts.append(f"{git_branch}{'*' if git_dirty else ''}")

    if segs.get("user", True):
        parts.append(user_display)

    show_agent = segs.get("agent", True)
    show_model = segs.get("model", True)
    if show_agent and show_model and model_str:
        parts.append(f"{agent_str}/{model_str}")
    elif show_agent:
        parts.append(agent_str)
    elif show_model and model_str:
        parts.append(model_str)

    if segs.get("task", True) and bound_task_id:
        parts.append(f"task:{bound_task_id}")

    if segs.get("leases", True):
        if active_claims:
            globs_preview: list[str] = []
            for claim in active_claims:
                for g in claim.get("claimedGlobs", []) or []:
                    globs_preview.append(str(g))
            preview_str = ", ".join(globs_preview[:2])
            if len(globs_preview) > 2:
                preview_str += f" +{len(globs_preview) - 2}"
            parts.append(f"leases:{len(active_claims)} ({preview_str})" if preview_str else f"leases:{len(active_claims)}")
        else:
            parts.append("leases:0")

    if segs.get("savepoints", True):
        if latest_savepoint:
            parts.append(f"snaps:{len(snapshots)} ({latest_savepoint})")
        else:
            parts.append(f"snaps:{len(snapshots)}")

    if segs.get("frozen", True):
        if frozen_globs:
            frz_preview = ", ".join(frozen_globs[:2])
            if len(frozen_globs) > 2:
                frz_preview += f" +{len(frozen_globs) - 2}"
            parts.append(f"frozen:{len(frozen_globs)} ({frz_preview})")
        else:
            parts.append("frozen:0")

    if segs.get("spool", True) and spool_count > 0:
        parts.append(f"spool:{spool_count}")

    line = "[" + " │ ".join(parts) + "]"
    return {
        "ok": True,
        "enabled": enabled,
        "workContext": work_bound,
        "authenticated": has_token,
        "modeBadge": mode_badge,
        "organizationId": config.organization_id if work_bound else None,
        "agencyOrganizationId": config.agency_organization_id if work_bound else None,
        "repository": repo_str,
        "projectName": config.project_name,
        "clientName": config.client_name,
        "gitBranch": git_branch,
        "gitDirty": git_dirty,
        "userEmail": user_email,
        "userName": user_name,
        "userDisplay": user_display,
        "userSource": user_source,
        "agent": agent_str,
        "model": model_str,
        "taskId": bound_task_id,
        "activeLeasesCount": len(active_claims),
        "savepointsCount": len(snapshots),
        "latestSavepoint": latest_savepoint,
        "frozenCount": len(frozen_globs),
        "spoolCount": spool_count,
        "segments": segs,
        "statusBar": line if (enabled or force) else "",
        "line": line,
    }


def format_status_bar_line(config: BridgeConfig) -> str:
    """Return the rendered status bar string if enabled, or empty string if disabled."""
    info = render_status_bar(config)
    return str(info.get("statusBar") or "")


def handle_status_bar(
    args: Any,
    config: BridgeConfig,
    *,
    force_json: bool = False,
    stdin_stream: Any = None,
    stdout_stream: Any = None,
) -> int:
    """CLI handler for `brg status-bar` / `brg statusbar` / `brg banner`."""
    hook_mode = bool(getattr(args, "hook", False))
    hook_payload: dict[str, Any] = {}
    if hook_mode:
        in_stream = stdin_stream if stdin_stream is not None else sys.stdin
        try:
            if not (hasattr(in_stream, "isatty") and in_stream.isatty()):
                raw_in = in_stream.read()
                if raw_in and raw_in.strip():
                    parsed = json.loads(raw_in)
                    if isinstance(parsed, dict):
                        hook_payload = parsed
        except Exception:
            hook_payload = {}

    cwd_override = getattr(args, "cwd", None)
    if not cwd_override and hook_payload:
        ws_paths = hook_payload.get("workspacePaths")
        if isinstance(ws_paths, list) and ws_paths and isinstance(ws_paths[0], str) and ws_paths[0].strip():
            cwd_override = ws_paths[0].strip()
        elif isinstance(hook_payload.get("cwd"), str) and hook_payload["cwd"].strip():
            cwd_override = hook_payload["cwd"].strip()

    if cwd_override:
        config = resolve_config(
            cwd=Path(cwd_override).resolve(),
            env=config.env,
            explicit_org=getattr(args, "org", None),
            explicit_agency_org=getattr(args, "agency_org", None),
            explicit_user=getattr(args, "user", None) or getattr(args, "global_user", None),
        )
    elif getattr(args, "user", None):
        config = replace(config, user_alias=str(args.user), user_name=str(args.user), user_source="flag")

    show_list: list[str] = []
    for item in getattr(args, "show", None) or []:
        show_list.extend(s.strip() for s in str(item).split(",") if s.strip())
    hide_list: list[str] = []
    for item in getattr(args, "hide", None) or []:
        hide_list.extend(s.strip() for s in str(item).split(",") if s.strip())
    only_raw = getattr(args, "only", None)
    only_list = [s.strip() for s in str(only_raw).split(",") if s.strip()] if only_raw else None
    reset_flag = bool(getattr(args, "reset", False))
    enable_flag = bool(getattr(args, "enable", False))
    disable_flag = bool(getattr(args, "disable", False))
    model_arg = getattr(args, "model", None)
    agent_arg = getattr(args, "agent", None)
    force_flag = bool(getattr(args, "force", False))

    if enable_flag or disable_flag or show_list or hide_list or only_list or reset_flag:
        enabled_arg: bool | None = None
        if enable_flag or disable_flag:
            enabled_arg = bool(enable_flag and not disable_flag)
        saved = save_status_bar_config(
            config,
            enabled=enabled_arg,
            model=model_arg,
            show_segments=show_list or None,
            hide_segments=hide_list or None,
            only_segments=only_list,
            reset_segments=reset_flag,
        )
        rendered = render_status_bar(
            config,
            explicit_agent=agent_arg,
            explicit_model=model_arg,
            force=True,
        )
        payload = {**saved, "rendered": rendered, "statusBar": rendered["line"]}
        state_word = "ENABLED" if saved["enabled"] else "DISABLED"
        human_lines = [f"Bridge Status Bar {state_word} ({saved['path']})"]
        if saved["enabled"]:
            human_lines.append(rendered["line"])
        emit_output(payload, human_lines, force_json=force_json, stdout_stream=stdout_stream)
        return EXIT_OK

    rendered = render_status_bar(
        config,
        explicit_agent=agent_arg,
        explicit_model=model_arg,
        force=force_flag,
    )
    target_out = stdout_stream or sys.stdout
    if hook_mode:
        line_out = str(rendered.get("statusBar") or "")
        inject = [{"ephemeralMessage": line_out}] if line_out else []
        target_out.write(json.dumps({"injectSteps": inject}) + "\n")
        target_out.flush()
        return EXIT_OK

    if force_json and not getattr(args, "text", False):
        target_out.write(json.dumps(rendered, indent=2, sort_keys=True) + "\n")
    else:
        line_out = rendered["statusBar"] if rendered.get("statusBar") else rendered["line"]
        if line_out:
            target_out.write(f"{line_out}\n")
    target_out.flush()
    return EXIT_OK
