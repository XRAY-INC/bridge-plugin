"""Tests for cross-worktree scope claims (claim, release, claims), overlap detection (exit code 2), and worktrees."""

from __future__ import annotations

import io
import json
import subprocess
from pathlib import Path
from typing import Any

from bridge_plugin.cli import run_cli
from bridge_plugin.runtime import EXIT_CONCURRENCY_BLOCK, EXIT_OK, globs_overlap


class NonTTYBuffer(io.StringIO):
    def isatty(self) -> bool:
        return False


def test_globs_overlap_matcher() -> None:
    assert globs_overlap("src/auth/*", "src/auth/login.ts") is True
    assert globs_overlap("src/**", "src/components/Button.tsx") is True
    assert globs_overlap("src/auth/*", "src/billing/*") is False
    assert globs_overlap("docs/*", "src/*") is False


def test_claim_conflict_between_agents_and_release(tmp_path: Path) -> None:
    """Verify claiming overlapping globs from a second agent returns exit code 2 (LEASE_CONFLICT)."""
    (tmp_path / ".bridge.json").write_text(
        json.dumps(
            {
                "plugin": "bridge",
                "organizationId": "org_northstar",
                "apiUrl": "https://bridge.xray.tech",
                "repository": "northstar/web-app",
            }
        ),
        encoding="utf-8",
    )
    base_env = {
        "BRIDGE_API_KEY": "xrkey_v1.concurrency_test",
        "BRIDGE_CREDENTIALS_PATH": str(tmp_path / "creds.json"),
    }

    def ok_transport(_req: Any, _timeout: float) -> tuple[int, bytes]:
        return 201, json.dumps({"ok": True}).encode("utf-8")

    # 1. Agent 'claude-code' claims src/auth/*
    out1 = NonTTYBuffer()
    code1 = run_cli(
        ["claim", "src/auth/*", "--ttl", "30m", "--agent", "claude-code", "--reason", "OAuth work"],
        cwd=tmp_path,
        env=base_env,
        stdout_stream=out1,
        transport=ok_transport,
    )
    assert code1 == EXIT_OK

    # 2. Agent 'cursor' attempts to claim overlapping src/auth/pkce.ts -> exits with code 2!
    out2 = NonTTYBuffer()
    code2 = run_cli(
        ["claim", "src/auth/pkce.ts", "--agent", "cursor"],
        cwd=tmp_path,
        env=base_env,
        stdout_stream=out2,
        transport=ok_transport,
    )
    assert code2 == EXIT_CONCURRENCY_BLOCK
    conflict_data = json.loads(out2.getvalue())
    assert conflict_data["error"] == "LEASE_CONFLICT"

    # 3. Agent 'cursor' claims disjoint src/billing/* -> succeeds (exit code 0)
    out3 = NonTTYBuffer()
    code3 = run_cli(
        ["claim", "src/billing/*", "--agent", "cursor"],
        cwd=tmp_path,
        env=base_env,
        stdout_stream=out3,
        transport=ok_transport,
    )
    assert code3 == EXIT_OK

    # 4. List active claims
    out_list = NonTTYBuffer()
    assert (
        run_cli(
            ["claims"],
            cwd=tmp_path,
            env=base_env,
            stdout_stream=out_list,
            transport=ok_transport,
        )
        == EXIT_OK
    )
    assert json.loads(out_list.getvalue())["count"] == 2

    # 5. Release claude-code's claim, now cursor can claim src/auth/*
    out_rel = NonTTYBuffer()
    assert (
        run_cli(
            ["release", "--agent", "claude-code"],
            cwd=tmp_path,
            env=base_env,
            stdout_stream=out_rel,
            transport=ok_transport,
        )
        == EXIT_OK
    )

    out4 = NonTTYBuffer()
    code4 = run_cli(
        ["claim", "src/auth/pkce.ts", "--agent", "cursor"],
        cwd=tmp_path,
        env=base_env,
        stdout_stream=out4,
        transport=ok_transport,
    )
    assert code4 == EXIT_OK


def test_worktree_create_list_and_rm(tmp_path: Path) -> None:
    """Verify `bridge worktree create` creates a linked git worktree and hydrates .bridge.json."""
    subprocess.run(["git", "init", "-b", "main"], cwd=str(tmp_path), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "dev@example.com"], cwd=str(tmp_path), check=True)
    subprocess.run(["git", "config", "user.name", "Dev"], cwd=str(tmp_path), check=True)
    (tmp_path / "README.md").write_text("# test\n", encoding="utf-8")
    (tmp_path / ".bridge.json").write_text(
        json.dumps({"plugin": "bridge", "organizationId": "org_wt", "repository": "acme/wt"}),
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "README.md"], cwd=str(tmp_path), check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=str(tmp_path), check=True, capture_output=True)

    env = {"BRIDGE_CREDENTIALS_PATH": str(tmp_path / "creds.json")}

    out_create = NonTTYBuffer()
    assert (
        run_cli(["worktree", "create", "track-ui"], cwd=tmp_path, env=env, stdout_stream=out_create)
        == EXIT_OK
    )
    wt_dir = tmp_path / ".worktrees" / "track-ui"
    assert wt_dir.is_dir()
    assert (wt_dir / ".bridge.json").is_file()

    out_list = NonTTYBuffer()
    assert run_cli(["worktree", "list"], cwd=tmp_path, env=env, stdout_stream=out_list) == EXIT_OK
    assert len(json.loads(out_list.getvalue())["worktrees"]) >= 2

    out_rm = NonTTYBuffer()
    assert run_cli(["worktree", "rm", "track-ui"], cwd=tmp_path, env=env, stdout_stream=out_rm) == EXIT_OK
    assert not wt_dir.exists()
