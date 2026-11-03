"""Tests for clean-room compliance, config resolution, runtime fingerprinting, TTY/JSON switching, and auth/setup commands."""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

from bridge_plugin.cli import run_cli
from bridge_plugin.config import resolve_config
from bridge_plugin.runtime import (
    EXIT_AUTH_MISSING,
    EXIT_OK,
    compute_idempotency_key,
    detect_runtime_context,
    emit_output,
    is_json_mode,
)


class FakeTTYBuffer(io.StringIO):
    def __init__(self, tty: bool = False) -> None:
        super().__init__()
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty


def test_clean_room_zero_legacy_tpx_or_tailscale_or_roster_references() -> None:
    """Verify zero tpx, Tailscale, SMB, Airtable recXXXX, or hardcoded XRAY roster strings in src/bridge_plugin."""
    src_root = Path(__file__).resolve().parents[1] / "src" / "bridge_plugin"
    forbidden_tokens = [
        "tpx",
        "tailscale",
        "tail3ef763",
        "smb://",
        "tree_sitter",
        "tree-sitter",
        "ed25519",
        "recXXXX",
        "airtable",
    ]
    py_files = list(src_root.rglob("*.py"))
    assert len(py_files) >= 8
    for py_file in py_files:
        content = py_file.read_text(encoding="utf-8").lower()
        for token in forbidden_tokens:
            assert token.lower() not in content, f"Forbidden legacy token '{token}' found in {py_file}"


def test_tty_vs_non_tty_json_auto_switching() -> None:
    """Verify TTY renders aligned tables while non-TTY or --json emits deterministic JSON."""
    payload = {"ok": True, "count": 2, "items": ["a", "b"]}
    human_table = "ID  NAME\n--  ----\n1   alpha"

    tty_buf = FakeTTYBuffer(tty=True)
    assert is_json_mode(force_json=False, stdout_stream=tty_buf) is False
    emit_output(payload, human_table, force_json=False, stdout_stream=tty_buf)
    assert "ID  NAME" in tty_buf.getvalue()
    assert not tty_buf.getvalue().strip().startswith("{")

    non_tty_buf = FakeTTYBuffer(tty=False)
    assert is_json_mode(force_json=False, stdout_stream=non_tty_buf) is True
    emit_output(payload, human_table, force_json=False, stdout_stream=non_tty_buf)
    parsed = json.loads(non_tty_buf.getvalue())
    assert parsed["ok"] is True
    assert parsed["count"] == 2

    forced_json_tty = FakeTTYBuffer(tty=True)
    emit_output(payload, human_table, force_json=True, stdout_stream=forced_json_tty)
    parsed_forced = json.loads(forced_json_tty.getvalue())
    assert parsed_forced["items"] == ["a", "b"]


def test_runtime_fingerprinting_agents_and_ci(tmp_path: Path) -> None:
    """Verify automatic detection of Claude Code, Cursor, Antigravity, Codex, and GitHub Actions."""
    ctx_claude = detect_runtime_context(
        cwd=tmp_path,
        env={"CLAUDECODE": "1", "ANTHROPIC_MODEL": "claude-sonnet-4-5"},
    )
    assert ctx_claude["agentId"] == "claude-code"
    assert ctx_claude["modelName"] == "claude-sonnet-4-5"

    ctx_cursor = detect_runtime_context(cwd=tmp_path, env={"CURSOR_TRACE_ID": "tr_123"})
    assert ctx_cursor["agentId"] == "cursor"

    ctx_agy = detect_runtime_context(cwd=tmp_path, env={"ANTIGRAVITY_CLI": "1"})
    assert ctx_agy["agentId"] == "antigravity"

    ctx_codex = detect_runtime_context(cwd=tmp_path, env={"CODEX_SANDBOX": "seatbelt"})
    assert ctx_codex["agentId"] == "codex"

    ctx_gha = detect_runtime_context(
        cwd=tmp_path,
        env={
            "GITHUB_ACTIONS": "true",
            "GITHUB_REPOSITORY": "acme/platform",
            "GITHUB_REF": "refs/pull/42/merge",
            "GITHUB_REF_NAME": "feat/pkce",
            "GITHUB_SHA": "deadbeef1234567890",
        },
    )
    assert ctx_gha["agentId"] == "github-actions"
    assert ctx_gha["ci"]["isCi"] is True
    assert ctx_gha["ci"]["provider"] == "github-actions"
    assert ctx_gha["ci"]["prNumber"] == 42
    assert ctx_gha["repository"] == "acme/platform"
    assert ctx_gha["gitBranch"] == "feat/pkce"

    # Key label resolution when no harness env var is set
    import base64

    claims_b64 = (
        base64.urlsafe_b64encode(
            json.dumps({"organizationId": "org_xray_tech", "label": "macbook-pro"}).encode("utf-8")
        )
        .decode("utf-8")
        .rstrip("=")
    )
    named_key = f"xrkey_v1.{claims_b64}.sig"
    ctx_key_label = detect_runtime_context(
        cwd=tmp_path,
        env={"BRIDGE_API_KEY": named_key, "BRIDGE_CREDENTIALS_PATH": str(tmp_path / "no-creds.json")},
    )
    assert ctx_key_label["agentId"] == "macbook-pro"
    assert ctx_key_label["keyLabel"] == "macbook-pro"


def test_idempotency_key_determinism() -> None:
    key1 = compute_idempotency_key("org_1", "acme/web", "main", "abc1234", "win", "Shipped OAuth")
    key2 = compute_idempotency_key("org_1", "acme/web", "main", "abc1234", "WIN", "  Shipped OAuth  ")
    key3 = compute_idempotency_key("org_1", "acme/web", "main", "abc1234", "decision", "Shipped OAuth")
    assert key1 == key2
    assert len(key1) == 64
    assert key1 != key3


def test_config_backward_compatibility_and_env_precedence(tmp_path: Path) -> None:
    """Verify .xray-dispatch.json and .workspace-project.json backward compatibility alongside .bridge.json."""
    legacy_file = tmp_path / ".xray-dispatch.json"
    legacy_file.write_text(
        json.dumps(
            {
                "plugin": "xray-dispatch",
                "organizationId": "org_legacy",
                "apiUrl": "https://bridge.xray.tech",
                "repository": "legacy/repo",
            }
        ),
        encoding="utf-8",
    )
    cfg_legacy = resolve_config(
        cwd=tmp_path,
        env={
            "XRAY_DISPATCH_API_KEY": "xrkey_v1.legacy_token_12345",
            "BRIDGE_CREDENTIALS_PATH": str(tmp_path / "creds.json"),
        },
    )
    assert cfg_legacy.organization_id == "org_legacy"
    assert cfg_legacy.repository == "legacy/repo"
    assert cfg_legacy.api_key == "xrkey_v1.legacy_token_12345"
    assert cfg_legacy.auth_source == "env:XRAY_DISPATCH_API_KEY"

    # Primary .bridge.json takes precedence over .xray-dispatch.json
    bridge_file = tmp_path / ".bridge.json"
    bridge_file.write_text(
        json.dumps(
            {
                "plugin": "bridge",
                "organizationId": "org_bridge_primary",
                "apiUrl": "https://bridge.xray.tech",
                "repository": "northstar/web-app",
            }
        ),
        encoding="utf-8",
    )
    cfg_primary = resolve_config(
        cwd=tmp_path,
        env={
            "BRIDGE_API_KEY": "xrkey_v1.primary_token_99999",
            "XRAY_DISPATCH_API_KEY": "xrkey_v1.legacy_token_12345",
            "BRIDGE_CREDENTIALS_PATH": str(tmp_path / "creds.json"),
        },
    )
    assert cfg_primary.organization_id == "org_bridge_primary"
    assert cfg_primary.repository == "northstar/web-app"
    assert cfg_primary.api_key == "xrkey_v1.primary_token_99999"
    assert cfg_primary.auth_source == "env:BRIDGE_API_KEY"


def test_auth_init_status_and_setup_commands(tmp_path: Path) -> None:
    """Verify semantic exit code 3 when unconfigured, then auth login, init, status, and setup."""
    cred_file = tmp_path / "credentials.json"
    env: dict[str, str] = {"BRIDGE_CREDENTIALS_PATH": str(cred_file)}

    # 1. Unconfigured workspace returns EXIT_AUTH_MISSING (3)
    out = FakeTTYBuffer(tty=False)
    code = run_cli(["status"], cwd=tmp_path, env=env, stdout_stream=out)
    assert code == EXIT_AUTH_MISSING
    assert json.loads(out.getvalue())["error"] == "AUTH_OR_CONFIG_MISSING"

    # 2. Login via stdin '-'
    out_login = FakeTTYBuffer(tty=False)
    code_login = run_cli(
        ["auth", "login", "--api-key", "-", "--org", "org_acme"],
        cwd=tmp_path,
        env=env,
        stdin_stream=io.StringIO("xrkey_v1.acme_secret_key_123456\n"),
        stdout_stream=out_login,
        transport=lambda _req, _timeout: (200, b'{"ok": true, "keys": []}'),
    )
    assert code_login == EXIT_OK
    assert cred_file.is_file()

    # 3. Init repository (.bridge.json)
    out_init = FakeTTYBuffer(tty=False)
    code_init = run_cli(
        ["init", "--org", "org_acme", "--repo", "acme/api"],
        cwd=tmp_path,
        env=env,
        stdout_stream=out_init,
    )
    assert code_init == EXIT_OK
    assert (tmp_path / ".bridge.json").is_file()

    # 4. Whoami & Status
    out_whoami = FakeTTYBuffer(tty=False)
    assert run_cli(["auth", "whoami"], cwd=tmp_path, env=env, stdout_stream=out_whoami) == EXIT_OK
    whoami_data = json.loads(out_whoami.getvalue())
    assert whoami_data["authenticated"] is True
    assert whoami_data["organizationId"] == "org_acme"

    out_status = FakeTTYBuffer(tty=False)
    assert run_cli(["status"], cwd=tmp_path, env=env, stdout_stream=out_status) == EXIT_OK
    status_data = json.loads(out_status.getvalue())
    assert status_data["repository"] == "acme/api"

    # 5. Setup targets (claude, cursor, git-hooks)
    for target in ("claude", "cursor", "antigravity", "codex", "git-hooks"):
        out_setup = FakeTTYBuffer(tty=False)
        assert run_cli(["setup", target], cwd=tmp_path, env=env, stdout_stream=out_setup) == EXIT_OK
    assert (tmp_path / ".mcp.json").is_file()
    assert (tmp_path / ".cursor" / "mcp.json").is_file()
    assert (tmp_path / ".claude" / "skills" / "bridge" / "SKILL.md").is_file()
    assert (tmp_path / ".git" / "hooks" / "prepare-commit-msg").is_file()

    # 6. Logout
    out_logout = FakeTTYBuffer(tty=False)
    assert run_cli(["auth", "logout"], cwd=tmp_path, env=env, stdout_stream=out_logout) == EXIT_OK
    assert not cred_file.exists()
