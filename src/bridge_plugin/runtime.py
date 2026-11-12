"""Runtime environment fingerprinting, Git inspection, TTY/JSON formatting, and idempotency keys."""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

# Semantic Exit Codes for Autonomous Loops & CI Runners
EXIT_OK = 0
EXIT_ERROR = 1
EXIT_CONCURRENCY_BLOCK = 2
EXIT_GOVERNANCE_BLOCK = 2
EXIT_AUTH_MISSING = 3
EXIT_CONFIG_MISSING = 3
EXIT_OFFLINE_SPOOLED = 4

DEFAULT_API_URL = "https://bridge.xray.tech"


def is_json_mode(force_json: bool = False, stdout_stream: Any = None) -> bool:
    """Return True when --json is set, BRIDGE_JSON=1, or stdout is not an interactive TTY."""
    if force_json:
        return True
    env_flag = os.environ.get("BRIDGE_JSON", "").strip().lower()
    if env_flag in ("1", "true", "yes"):
        return True
    stream = stdout_stream if stdout_stream is not None else sys.stdout
    isatty_fn = getattr(stream, "isatty", None)
    if callable(isatty_fn):
        try:
            return not bool(isatty_fn())
        except Exception:
            return True
    return True


def emit_stderr(message: str, stderr_stream: Any = None) -> None:
    """Write warnings, spool notices, and diagnostics strictly to stderr."""
    stream = stderr_stream if stderr_stream is not None else sys.stderr
    stream.write(f"[bridge] {message}\n")
    flush_fn = getattr(stream, "flush", None)
    if callable(flush_fn):
        flush_fn()


def format_table(headers: Sequence[str], rows: Sequence[Sequence[Any]]) -> str:
    """Render an aligned plain-text table for interactive TTY sessions."""
    if not rows:
        return "(no records)"
    str_headers = [str(h) for h in headers]
    str_rows = [[str(cell if cell is not None else "") for cell in row] for row in rows]
    widths = [len(h) for h in str_headers]
    for row in str_rows:
        for idx, val in enumerate(row):
            if idx < len(widths):
                widths[idx] = max(widths[idx], len(val))
    header_line = "  ".join(h.ljust(widths[i]) for i, h in enumerate(str_headers))
    sep_line = "  ".join("-" * widths[i] for i in range(len(str_headers)))
    body_lines = [
        "  ".join(
            (row[i] if i < len(row) else "").ljust(widths[i])
            for i in range(len(str_headers))
        ).rstrip()
        for row in str_rows
    ]
    return "\n".join([header_line, sep_line, *body_lines])


def emit_output(
    payload: Mapping[str, Any],
    human_output: str | Sequence[str] | None = None,
    *,
    force_json: bool = False,
    stdout_stream: Any = None,
) -> None:
    """Emit deterministic JSON on non-TTY / --json, or human-readable output on interactive TTY."""
    stream = stdout_stream if stdout_stream is not None else sys.stdout
    if is_json_mode(force_json=force_json, stdout_stream=stream):
        stream.write(json.dumps(payload, indent=2, default=str) + "\n")
    else:
        if human_output is None:
            stream.write(json.dumps(payload, indent=2, default=str) + "\n")
        elif isinstance(human_output, str):
            stream.write(human_output.rstrip("\n") + "\n")
        else:
            stream.write("\n".join(str(line) for line in human_output).rstrip("\n") + "\n")
    flush_fn = getattr(stream, "flush", None)
    if callable(flush_fn):
        flush_fn()


def resolve_stdin_or_arg(value: str | None, stdin_stream: Any = None) -> str:
    """Resolve '-' from stdin or return the stripped argument string."""
    if value == "-":
        stream = stdin_stream if stdin_stream is not None else sys.stdin
        return str(stream.read()).strip()
    return (value or "").strip()


def compute_idempotency_key(
    org_id: str,
    repo: str,
    branch: str,
    commit_sha: str | None,
    tag: str,
    summary: str,
) -> str:
    """Compute a deterministic SHA-256 client-side idempotency key for mutation replay."""
    canonical = "|".join(
        [
            (org_id or "").strip(),
            (repo or "").strip(),
            (branch or "main").strip(),
            (commit_sha or "").strip(),
            (tag or "").strip().upper(),
            (summary or "").strip(),
        ]
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def copy_file_safe(src: Path, dst: Path) -> None:
    """Copy file content and basic metadata without failing on SMB/NFS superuser chflags (e.g. SF_ARCHIVED)."""
    shutil.copyfile(src, dst)
    try:
        shutil.copystat(src, dst)
    except OSError:
        try:
            st = src.stat()
            os.utime(dst, ns=(st.st_atime_ns, st.st_mtime_ns))
            shutil.copymode(src, dst)
        except OSError:
            pass


def _run_git(args: Sequence[str], cwd: Path | None = None) -> str | None:
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=3,
            check=False,
        )
        if proc.returncode == 0:
            return proc.stdout.strip()
    except Exception:
        pass
    return None


def detect_git_root(cwd: Path | None = None) -> Path | None:
    """Return the Git top-level directory if inside a Git working tree."""
    out = _run_git(["rev-parse", "--show-toplevel"], cwd=cwd)
    if out:
        return Path(out)
    return None


def parse_remote_url_to_slug(remote_url: str) -> str | None:
    """Extract 'owner/repo' from HTTPS or SSH git remote URLs."""
    cleaned = remote_url.strip()
    if not cleaned:
        return None
    if cleaned.endswith(".git"):
        cleaned = cleaned[:-4]
    ssh_match = re.match(r"^git@[^:]+:([^/]+/[^/]+)$", cleaned)
    if ssh_match:
        return ssh_match.group(1)
    https_match = re.search(r"[:/]([^/:]+/[^/:]+)$", cleaned)
    if https_match:
        return https_match.group(1)
    return None


def detect_git_repo_slug(
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> str:
    """Detect repository slug ('owner/repo') from env, git remote, or directory name."""
    environ = env if env is not None else os.environ
    for env_key in ("BRIDGE_REPO", "GITHUB_REPOSITORY", "CI_PROJECT_PATH"):
        val = environ.get(env_key, "").strip()
        if val:
            return val
    remote_url = _run_git(["config", "--get", "remote.origin.url"], cwd=cwd)
    if remote_url:
        slug = parse_remote_url_to_slug(remote_url)
        if slug:
            return slug
    root = detect_git_root(cwd=cwd) or (cwd or Path.cwd())
    return root.name or "workspace-repo"


def detect_git_branch(
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> str:
    """Detect active git branch from env or git rev-parse."""
    environ = env if env is not None else os.environ
    for env_key in (
        "BRIDGE_BRANCH",
        "GITHUB_HEAD_REF",
        "GITHUB_REF_NAME",
        "CI_COMMIT_REF_NAME",
        "RAILWAY_GIT_BRANCH",
        "VERCEL_GIT_COMMIT_REF",
    ):
        val = environ.get(env_key, "").strip()
        if val:
            return val
    branch = _run_git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=cwd)
    if branch and branch != "HEAD":
        return branch
    return "main"


def detect_git_commit_sha(
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> str | None:
    """Detect current commit SHA from env or git rev-parse."""
    environ = env if env is not None else os.environ
    for env_key in (
        "BRIDGE_COMMIT_SHA",
        "GITHUB_SHA",
        "CI_COMMIT_SHA",
        "RAILWAY_GIT_COMMIT_SHA",
        "VERCEL_GIT_COMMIT_SHA",
    ):
        val = environ.get(env_key, "").strip()
        if val:
            return val
    sha = _run_git(["rev-parse", "HEAD"], cwd=cwd)
    return sha if sha else None


def _extract_key_label(api_key: str | None) -> str | None:
    """Extract the custom key name/label from an xrkey_v1 token if present."""
    parts = (api_key or "").strip().split(".")
    if len(parts) < 2 or parts[0] != "xrkey_v1":
        return None
    try:
        import base64

        padded = parts[1] + "=" * (-len(parts[1]) % 4)
        claims = json.loads(base64.urlsafe_b64decode(padded))
        if isinstance(claims, dict):
            label = claims.get("label")
            if isinstance(label, str) and label.strip() and label.strip() != "bridge-cli":
                return label.strip()
    except Exception:
        return None
    return None


def _resolve_api_key_label(
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> str | None:
    """Resolve the active API key's custom label from env, project config, or user credentials."""
    environ = env if env is not None else os.environ
    for env_name in ("BRIDGE_API_KEY", "WORKSPACE_API_KEY"):
        val = environ.get(env_name, "").strip()
        if val:
            return _extract_key_label(val)

    work_dir = (cwd or Path.cwd()).resolve()
    for directory in (work_dir, *work_dir.parents):
        for filename in (".bridge.json", ".workspace-project.json"):
            candidate = directory / filename
            if candidate.is_file():
                try:
                    data = json.loads(candidate.read_text(encoding="utf-8"))
                    if isinstance(data, dict) and isinstance(data.get("apiKey"), str):
                        label = _extract_key_label(data["apiKey"])
                        if label:
                            return label
                except Exception:
                    pass
        if (directory / ".git").exists():
            break

    override = environ.get("BRIDGE_CREDENTIALS_PATH", "").strip()
    xdg = environ.get("XDG_CONFIG_HOME", "").strip()
    if override:
        cred_path: Path | None = Path(override)
    elif xdg:
        cred_path = Path(xdg) / "bridge" / "credentials.json"
    elif env is None or env is os.environ:
        cred_path = Path.home() / ".config" / "bridge" / "credentials.json"
    else:
        cred_path = None

    if cred_path and cred_path.is_file():
        try:
            creds = json.loads(cred_path.read_text(encoding="utf-8"))
            if isinstance(creds, dict) and isinstance(creds.get("apiKey"), str):
                return _extract_key_label(creds["apiKey"])
        except Exception:
            pass
    return None


def detect_runtime_context(
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
    explicit_agent: str | None = None,
) -> dict[str, Any]:
    """Auto-fingerprint AI coding agent harness, custom API key name, and CI runner environment."""
    environ = env if env is not None else os.environ
    key_label = _resolve_api_key_label(cwd=cwd, env=environ)

    # 1. Detect AI Agent / Key Identity
    agent_id = (explicit_agent or environ.get("BRIDGE_AGENT_ID") or "").strip()
    if not agent_id:
        if key_label:
            agent_id = key_label
        elif environ.get("CLAUDECODE") == "1" or environ.get("CLAUDE_CODE_ENTRYPOINT"):
            agent_id = "claude-code"
        elif environ.get("CURSOR_TRACE_ID") or environ.get("CURSOR_SESSION_ID"):
            agent_id = "cursor"
        elif environ.get("ANTIGRAVITY_CLI") or environ.get("ANTIGRAVITY_AGENT"):
            agent_id = "antigravity"
        elif environ.get("CODEX_SANDBOX") or environ.get("OPENAI_CODEX"):
            agent_id = "codex"
        elif environ.get("GITHUB_ACTIONS") == "true":
            agent_id = "github-actions"
        elif environ.get("GITLAB_CI"):
            agent_id = "gitlab-ci"
        elif environ.get("RAILWAY_ENVIRONMENT_NAME"):
            agent_id = "railway-ci"
        elif environ.get("VERCEL"):
            agent_id = "vercel-ci"
        else:
            agent_id = "bridge-cli"

    model_name = (
        environ.get("BRIDGE_MODEL")
        or environ.get("ANTHROPIC_MODEL")
        or environ.get("OPENAI_MODEL")
        or environ.get("GEMINI_MODEL")
        or None
    )

    # 2. Detect CI Runner Metadata
    ci_provider: str | None = None
    pr_number: int | None = None
    ci_env_name: str | None = None

    if environ.get("GITHUB_ACTIONS") == "true":
        ci_provider = "github-actions"
        ref = environ.get("GITHUB_REF", "")
        pr_match = re.match(r"^refs/pull/(\d+)/", ref)
        if pr_match:
            pr_number = int(pr_match.group(1))
    elif environ.get("GITLAB_CI"):
        ci_provider = "gitlab-ci"
        mr_iid = environ.get("CI_MERGE_REQUEST_IID", "")
        if mr_iid.isdigit():
            pr_number = int(mr_iid)
    elif environ.get("RAILWAY_ENVIRONMENT_NAME"):
        ci_provider = "railway"
        ci_env_name = environ.get("RAILWAY_ENVIRONMENT_NAME")
    elif environ.get("VERCEL"):
        ci_provider = "vercel"
        ci_env_name = environ.get("VERCEL_ENV")

    repo = detect_git_repo_slug(cwd=cwd, env=environ)
    branch = detect_git_branch(cwd=cwd, env=environ)
    commit_sha = detect_git_commit_sha(cwd=cwd, env=environ)

    try:
        hostname = socket.gethostname()
    except Exception:
        hostname = "localhost"

    return {
        "agentId": agent_id,
        "keyLabel": key_label,
        "modelName": model_name,
        "repository": repo,
        "gitBranch": branch,
        "commitSha": commit_sha,
        "hostname": hostname,
        "ci": {
            "isCi": ci_provider is not None or environ.get("CI") in ("true", "1"),
            "provider": ci_provider,
            "repository": environ.get("GITHUB_REPOSITORY") or repo,
            "branch": branch,
            "commitSha": commit_sha,
            "prNumber": pr_number,
            "environment": ci_env_name,
        },
    }


def _normalize_glob(pattern: str) -> str:
    p = pattern.strip().replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    p = p.lstrip("/")
    return p


def _glob_prefix(pattern: str) -> str:
    norm = _normalize_glob(pattern)
    for idx, ch in enumerate(norm):
        if ch in "*?[":
            return norm[:idx].rstrip("/")
    return norm.rstrip("/")


def globs_overlap(glob_a: str, glob_b: str) -> bool:
    """Return True if two file/directory glob patterns overlap."""
    a = _normalize_glob(glob_a)
    b = _normalize_glob(glob_b)
    if not a or not b:
        return False
    if a == b or a in ("*", "**", "**/*") or b in ("*", "**", "**/*"):
        return True
    if fnmatch.fnmatchcase(a, b) or fnmatch.fnmatchcase(b, a):
        return True
    pref_a = _glob_prefix(a)
    pref_b = _glob_prefix(b)
    if pref_a and pref_b:
        if pref_a == pref_b:
            return True
        if pref_a.startswith(pref_b + "/") and ("*" in b or "?" in b):
            return True
        if pref_b.startswith(pref_a + "/") and ("*" in a or "?" in a):
            return True
    return False
