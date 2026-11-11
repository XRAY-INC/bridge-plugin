"""Tests for clean-room compliance, config resolution, runtime fingerprinting, TTY/JSON switching, and auth/setup commands."""

from __future__ import annotations

import hashlib
import io
import json
import re
from pathlib import Path

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

FORBIDDEN_TOKEN_SHA256 = frozenset(
    {
        "d28e29a3443944bc1b81e0978fdcc0267b5cd73ef1a975e85a9f98a345af5f9e",
        "455a3802cbd5d41412791694255a1ddb391f18716588fa6331a199100ec324ed",
        "daf2f0a99b03eba2558f8ef7fff70ed21bf1a0f67ec4b6109e6beb2040740528",
        "795b104abe3e4134960ca245ded0e617f162c751209347b7f003bc35e062f43e",
        "8d3cdf0429d36991a00ff96938768c1e152718a168f50ad98688abe4a2a4d21e",
        "ab95687082dab0699c5b2bb9ececd9dfe0913974a67dc6ddd90ac5ca33aed09d",
        "efedd77bf8fe92187a6906dd902d3dc755b9fe1de4f9814c277f38202bd17284",
        "cb0df75f87c96c3df84a53e6bd2213a0ace54591170cbfd1622cdd68ee417ca7",
        "d0f712edbe6c5be1567f0c054ef18cc0e768f11c72d2df7c733746dab53694ad",
        "60b6711f0f72bdd6484fa68b26e196efa4a76a410e8117d1df9cfa17c6f5354e",
        "bdec0966ba1f220221cd5d68df4686e7633093ffef703e255027b7c6df3717a2",
        "d1304d0fb7692419f2f9647c6868f2e7df369536d2cc741da8c37b4f0f8b8df5",
        "70e153339fa55bd0d3de0745c98c21fcdd9aa347b0a2c1f4553b8d0d253c41c9",
        "8dbf2b7197ab674af4f63f30def74063c12c501183e725bda9557b8c87dd1238",
        "36412cb40b1a491949000a4ef8ee5887da8613063cf4883544853637615b8192",
        "8f880775b0ae49b0eea10e30ebe1bac033acd80431ef63c585b76a9b495c0849",
        "d184515423a7c39c5a9b18c2855e1b54056ee3672ac966f56a34c16f6fd12724",
        "63bf282636daf1cceb2c79a34e321a29bf53b85949fd7cc939a5ce495020dbd1",
        "7d0dc28f8fd95654cf0704adc0bd3f33ee171a8fc48861f345c27cdf0aa8f65c",
        "617391aba2d2dba66d49556a1480dc51c97fac63361eba8596c816b3e00d98e9",
    }
)


def assert_clean_room_tree(repo_root: Path) -> None:
    """Scan all source, skill, manifest, hook, script, and test files against SHA-256 forbidden digests."""
    candidates: list[Path] = []
    for sub in ("src", "skills", "commands", "hooks", "tests", ".claude-plugin", ".agents"):
        sub_dir = repo_root / sub
        if sub_dir.exists():
            candidates.extend(
                p for p in sub_dir.rglob("*") if p.is_file() and "__pycache__" not in p.parts
            )
    for top in ("README.md", "LICENSE", "install.sh", "pyproject.toml"):
        top_path = repo_root / top
        if top_path.is_file():
            candidates.append(top_path)

    assert len(candidates) >= 25
    assert not (repo_root / ".bridge.json").exists(), ".bridge.json must not be tracked in public SDK"

    token_re = re.compile(r"[a-z0-9_@.-]+")
    for file_path in candidates:
        text = file_path.read_text(encoding="utf-8").lower()
        for raw in token_re.findall(text):
            cleaned = raw.strip(".-")
            variants = {cleaned}
            if "@" not in cleaned and "." in cleaned:
                variants.update(part for part in cleaned.split(".") if part)
            for candidate in variants:
                digest = hashlib.sha256(candidate.encode("utf-8")).hexdigest()
                assert digest not in FORBIDDEN_TOKEN_SHA256, (
                    f"Clean-room violation in {file_path.relative_to(repo_root)} (sha256={digest[:12]})"
                )


class FakeTTYBuffer(io.StringIO):
    def __init__(self, tty: bool = False) -> None:
        super().__init__()
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty


def test_clean_room_zero_internal_identifiers_across_entire_sdk() -> None:
    """Verify zero internal identifiers, staff handles, or legacy internal tooling across the entire SDK."""
    repo_root = Path(__file__).resolve().parents[1]
    assert_clean_room_tree(repo_root)


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
            json.dumps({"organizationId": "org_northstar", "label": "macbook-pro"}).encode("utf-8")
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
    """Verify .workspace-project.json and WORKSPACE_API_KEY fallback alongside .bridge.json."""
    legacy_file = tmp_path / ".workspace-project.json"
    legacy_file.write_text(
        json.dumps(
            {
                "plugin": "workspace-project",
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
            "WORKSPACE_API_KEY": "xrkey_v1.legacy_token_12345",
            "BRIDGE_CREDENTIALS_PATH": str(tmp_path / "creds.json"),
        },
    )
    assert cfg_legacy.organization_id == "org_legacy"
    assert cfg_legacy.repository == "legacy/repo"
    assert cfg_legacy.api_key == "xrkey_v1.legacy_token_12345"
    assert cfg_legacy.auth_source == "env:WORKSPACE_API_KEY"

    # Primary .bridge.json takes precedence over .workspace-project.json
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
            "WORKSPACE_API_KEY": "xrkey_v1.legacy_token_12345",
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
