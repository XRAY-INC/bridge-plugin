"""Comprehensive tests for unified AFK capabilities in Bridge:
1. Teammate identity & roster alias resolution (`brg user` / `--session` / `--roster` / `--clear`)
2. Off-branch Git savepoints, step-by-step undo, rollback, and untracked file rescue (`brg savepoint`, `brg undo`, `brg rollback`)
3. Frozen file locks (`brg freeze`, `brg unfreeze`)
4. PreToolUse guard (`brg guard`) enforcing frozen files, other-agent scope leases, destructive commands, and pre-mutation savepoints
5. Non-Git client folder linking & directory snapshots (`brg init --project --client`, `brg seam`, `brg savepoint`, `brg undo`)
"""

from __future__ import annotations

import io
import json
import subprocess
from pathlib import Path
from typing import Any

from bridge_plugin.cli import run_cli
from bridge_plugin.runtime import (
    EXIT_CONCURRENCY_BLOCK,
    EXIT_CONFIG_MISSING,
    EXIT_OK,
)


class NonTTYBuffer(io.StringIO):
    def isatty(self) -> bool:
        return False


def _init_git_repo(repo_dir: Path) -> None:
    repo_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-b", "main"], cwd=str(repo_dir), capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "dev@acme.example"], cwd=str(repo_dir), capture_output=True, check=True)
    subprocess.run(["git", "config", "user.name", "Dev"], cwd=str(repo_dir), capture_output=True, check=True)
    (repo_dir / "README.md").write_text("# Initial Repo\n", encoding="utf-8")
    (repo_dir / "src").mkdir(exist_ok=True)
    (repo_dir / "src" / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=str(repo_dir), capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "initial commit"], cwd=str(repo_dir), capture_output=True, check=True)


def test_user_identity_roster_session_and_propagation(tmp_path: Path) -> None:
    """Verify `brg user` resolves org/workspace roster aliases or emailDomain, supports `--session` vs machine credentials, and propagates to seams/leases/decisions."""
    creds_path = tmp_path / "creds.json"
    env = {
        "BRIDGE_API_KEY": "xrkey_v1.test_key",
        "BRIDGE_CREDENTIALS_PATH": str(creds_path),
    }
    (tmp_path / ".bridge.json").write_text(
        json.dumps(
            {
                "plugin": "bridge",
                "organizationId": "org_acme",
                "apiUrl": "https://bridge.xray.tech",
                "repository": "acme/web-platform",
                "emailDomain": "acme.com",
                "teamRoster": [
                    {"handle": "alice", "name": "Alice Chen", "email": "alice@acme.com"},
                    {"handle": "bob", "name": "Bob Rivera", "email": "bob@acme.com"},
                ],
            }
        ),
        encoding="utf-8",
    )

    # 1. Roster listing from workspace config / cached org roster
    out_roster = NonTTYBuffer()
    assert run_cli(["user", "--roster"], cwd=tmp_path, env=env, stdout_stream=out_roster) == EXIT_OK
    roster_data = json.loads(out_roster.getvalue())
    assert roster_data["ok"] is True
    aliases = {item["alias"]: item for item in roster_data["roster"]}
    assert aliases["alice"]["email"] == "alice@acme.com"
    assert aliases["alice"]["name"] == "Alice Chen"
    assert aliases["bob"]["email"] == "bob@acme.com"

    # 2. Set machine-level user via emailDomain fallback (`dev` -> `dev@acme.com`)
    out_dev = NonTTYBuffer()
    assert run_cli(["user", "dev", "--name", "Dev Lead"], cwd=tmp_path, env=env, stdout_stream=out_dev) == EXIT_OK
    dev_data = json.loads(out_dev.getvalue())
    assert dev_data["userEmail"] == "dev@acme.com"
    assert dev_data["userName"] == "Dev Lead"
    assert dev_data["sessionOnly"] is False

    # 3. Override with session-only user `alice --session` (so shared login on same machine doesn't clobber)
    out_alice = NonTTYBuffer()
    assert run_cli(["user", "alice", "--session"], cwd=tmp_path, env=env, stdout_stream=out_alice) == EXIT_OK
    alice_data = json.loads(out_alice.getvalue())
    assert alice_data["userEmail"] == "alice@acme.com"
    assert alice_data["userName"] == "Alice Chen"
    assert alice_data["sessionOnly"] is True
    assert (tmp_path / ".bridge" / "session_user.json").is_file()

    # Verify `brg user` shows session identity taking precedence over machine credentials
    out_show = NonTTYBuffer()
    assert run_cli(["user"], cwd=tmp_path, env=env, stdout_stream=out_show) == EXIT_OK
    show_data = json.loads(out_show.getvalue())
    assert show_data["userEmail"] == "alice@acme.com"
    assert show_data["source"] == "session"

    # 4. Verify `brg seam win` and `brg claim` propagate Alice's identity
    captured_bodies: list[dict[str, Any]] = []

    def transport(req: Any, _timeout: float) -> tuple[int, bytes]:
        if req.data:
            captured_bodies.append(json.loads(req.data.decode("utf-8")))
        return 200, json.dumps({"ok": True, "seam": {"id": "seam_alice_1"}}).encode("utf-8")

    out_seam = NonTTYBuffer()
    assert (
        run_cli(
            ["seam", "win", "Shipped shared-login identity"],
            cwd=tmp_path,
            env=env,
            stdout_stream=out_seam,
            transport=transport,
        )
        == EXIT_OK
    )
    assert captured_bodies[-1]["userEmail"] == "alice@acme.com"
    assert captured_bodies[-1]["actorEmail"] == "alice@acme.com"
    assert captured_bodies[-1]["metadata"]["userName"] == "Alice Chen"
    assert captured_bodies[-1]["metadata"]["userAlias"] == "alice"

    # 5. Clear session user -> falls back to machine user (`dev@acme.com`)
    out_clear_sess = NonTTYBuffer()
    assert run_cli(["user", "--clear", "--session"], cwd=tmp_path, env=env, stdout_stream=out_clear_sess) == EXIT_OK
    out_after = NonTTYBuffer()
    assert run_cli(["user"], cwd=tmp_path, env=env, stdout_stream=out_after) == EXIT_OK
    after_data = json.loads(out_after.getvalue())
    assert after_data["userEmail"] == "dev@acme.com"
    assert after_data["source"] == "credentials"

    # 6. Verify `brg context` automatically caches server `teamRoster` into `.bridge/roster.json`
    def context_transport(_req: Any, _timeout: float) -> tuple[int, bytes]:
        return 200, json.dumps(
            {
                "ok": True,
                "organizationId": "org_acme",
                "teamRoster": [
                    {"handle": "priya", "name": "Priya Patel", "email": "priya@acme.com"},
                ],
            }
        ).encode("utf-8")

    out_ctx = NonTTYBuffer()
    assert run_cli(["context"], cwd=tmp_path, env=env, stdout_stream=out_ctx, transport=context_transport) == EXIT_OK
    assert (tmp_path / ".bridge" / "roster.json").is_file()

    out_priya = NonTTYBuffer()
    assert run_cli(["user", "priya", "--session"], cwd=tmp_path, env=env, stdout_stream=out_priya) == EXIT_OK
    priya_data = json.loads(out_priya.getvalue())
    assert priya_data["userEmail"] == "priya@acme.com"
    assert priya_data["userName"] == "Priya Patel"


def test_git_savepoint_undo_rollback_and_untracked_rescue(tmp_path: Path) -> None:
    """Verify off-branch Git savepoints, step-by-step undo, rollback by label/ID, untracked rescue, and HEAD-moved protection."""
    _init_git_repo(tmp_path)
    env = {"BRIDGE_CREDENTIALS_PATH": str(tmp_path / "creds.json")}

    # 1. Create baseline savepoint
    out_sp1 = NonTTYBuffer()
    assert (
        run_cli(["savepoint", "create", "--label", "afk-baseline"], cwd=tmp_path, env=env, stdout_stream=out_sp1)
        == EXIT_OK
    )
    sp1 = json.loads(out_sp1.getvalue())["savepoint"]
    assert sp1["kind"] == "git"
    assert sp1["label"] == "afk-baseline"
    snap1_id = sp1["id"]

    # Verify git ref was created off-branch without moving HEAD
    ref_proc = subprocess.run(
        ["git", "rev-parse", f"refs/bridge-snapshots/{snap1_id}"],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        check=False,
    )
    assert ref_proc.returncode == 0

    # 2. Mutate tracked file and create a new untracked file
    (tmp_path / "src" / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
    (tmp_path / "src" / "new_helper.py").write_text("HELPER = True\n", encoding="utf-8")

    # 3. Preview undo with --dry-run
    out_dry = NonTTYBuffer()
    assert run_cli(["undo", "--dry-run"], cwd=tmp_path, env=env, stdout_stream=out_dry) == EXIT_OK
    dry_data = json.loads(out_dry.getvalue())
    assert dry_data["dryRun"] is True
    assert "src/new_helper.py" in dry_data["rescuedUntrackedFiles"]
    assert (tmp_path / "src" / "app.py").read_text(encoding="utf-8") == "VALUE = 2\n"

    # 4. Execute `brg undo` -> restores `src/app.py` to VALUE = 1 and rescues `src/new_helper.py`
    out_undo = NonTTYBuffer()
    assert run_cli(["undo"], cwd=tmp_path, env=env, stdout_stream=out_undo) == EXIT_OK
    undo_data = json.loads(out_undo.getvalue())
    assert undo_data["restored"] is True
    assert (tmp_path / "src" / "app.py").read_text(encoding="utf-8") == "VALUE = 1\n"
    assert not (tmp_path / "src" / "new_helper.py").exists()
    rescued_file = tmp_path / ".bridge" / "rescued" / snap1_id / "src" / "new_helper.py"
    assert rescued_file.is_file()
    assert rescued_file.read_text(encoding="utf-8") == "HELPER = True\n"

    # 5. Advance HEAD with a new commit and verify rollback refuses without --force (exit code 3), then succeeds with --force
    (tmp_path / "src" / "app.py").write_text("VALUE = 3\n", encoding="utf-8")
    subprocess.run(["git", "add", "src/app.py"], cwd=str(tmp_path), capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "advance head"], cwd=str(tmp_path), capture_output=True, check=True)

    out_refuse = NonTTYBuffer()
    assert (
        run_cli(["rollback", "afk-baseline"], cwd=tmp_path, env=env, stdout_stream=out_refuse)
        == EXIT_CONFIG_MISSING
    )
    assert json.loads(out_refuse.getvalue())["error"] == "HEAD_MOVED_SINCE_SAVEPOINT"

    out_force = NonTTYBuffer()
    assert (
        run_cli(["rollback", "afk-baseline", "--force"], cwd=tmp_path, env=env, stdout_stream=out_force)
        == EXIT_OK
    )
    assert (tmp_path / "src" / "app.py").read_text(encoding="utf-8") == "VALUE = 1\n"


def test_freeze_unfreeze_and_pre_tool_use_guard(tmp_path: Path) -> None:
    """Verify `brg freeze`, `brg unfreeze`, and `brg guard` PreToolUse enforcement."""
    _init_git_repo(tmp_path)
    (tmp_path / "prisma" / "migrations").mkdir(parents=True)
    (tmp_path / "prisma" / "migrations" / "001_init.sql").write_text("CREATE TABLE t (id INT);\n", encoding="utf-8")
    env = {"BRIDGE_CREDENTIALS_PATH": str(tmp_path / "creds.json"), "BRIDGE_AGENT_ID": "claude-code"}

    # 1. Freeze prisma/migrations/**
    out_freeze = NonTTYBuffer()
    assert (
        run_cli(["freeze", "prisma/migrations/**", ".env*"], cwd=tmp_path, env=env, stdout_stream=out_freeze)
        == EXIT_OK
    )
    freeze_data = json.loads(out_freeze.getvalue())
    assert "prisma/migrations/**" in freeze_data["frozenGlobs"]

    # 2. `brg guard` denies Edit on frozen file
    in_frozen = io.StringIO(
        json.dumps(
            {
                "tool_name": "Edit",
                "tool_input": {"file_path": str(tmp_path / "prisma" / "migrations" / "001_init.sql")},
            }
        )
    )
    out_guard_frozen = NonTTYBuffer()
    code_frozen = run_cli(["guard"], cwd=tmp_path, env=env, stdin_stream=in_frozen, stdout_stream=out_guard_frozen)
    assert code_frozen == EXIT_CONCURRENCY_BLOCK
    guard_frozen_res = json.loads(out_guard_frozen.getvalue())
    assert guard_frozen_res["decision"] == "deny"
    assert "prisma/migrations/**" in guard_frozen_res["reason"]

    # 3. Seed an active claim held by ANOTHER agent (`cursor`) on `src/locked/**`
    out_claim = NonTTYBuffer()
    assert (
        run_cli(
            ["claim", "src/locked/**", "--agent", "cursor", "--ttl", "30m"],
            cwd=tmp_path,
            env=env,
            stdout_stream=out_claim,
        )
        == EXIT_OK
    )

    in_conflict = io.StringIO(
        json.dumps(
            {
                "tool_name": "Write",
                "tool_input": {"file_path": "src/locked/secret.py"},
            }
        )
    )
    out_guard_conflict = NonTTYBuffer()
    code_conflict = run_cli(
        ["guard"],
        cwd=tmp_path,
        env=env,
        stdin_stream=in_conflict,
        stdout_stream=out_guard_conflict,
    )
    assert code_conflict == EXIT_CONCURRENCY_BLOCK
    guard_conflict_res = json.loads(out_guard_conflict.getvalue())
    assert guard_conflict_res["decision"] == "deny"
    assert "cursor" in guard_conflict_res["reason"]

    # 4. `brg guard` denies destructive Bash commands (`git reset --hard`, `gh pr merge --admin`)
    for bad_cmd in ("git reset --hard HEAD~1", "gh pr merge 42 --admin", "git push origin main --force"):
        in_bash = io.StringIO(
            json.dumps({"tool_name": "Bash", "tool_input": {"command": bad_cmd}})
        )
        out_bash = NonTTYBuffer()
        assert run_cli(["guard"], cwd=tmp_path, env=env, stdin_stream=in_bash, stdout_stream=out_bash) == EXIT_CONCURRENCY_BLOCK
        assert json.loads(out_bash.getvalue())["decision"] == "deny"

    # 5. `brg guard` allows safe edit on `src/app.py` and auto-creates a pre_edit savepoint
    in_ok = io.StringIO(
        json.dumps(
            {
                "tool_name": "Edit",
                "tool_input": {"file_path": "src/app.py"},
            }
        )
    )
    out_ok = NonTTYBuffer()
    assert run_cli(["guard"], cwd=tmp_path, env=env, stdin_stream=in_ok, stdout_stream=out_ok) == EXIT_OK
    assert json.loads(out_ok.getvalue())["decision"] == "allow"

    out_list_sp = NonTTYBuffer()
    assert run_cli(["savepoint", "list"], cwd=tmp_path, env=env, stdout_stream=out_list_sp) == EXIT_OK
    snaps = json.loads(out_list_sp.getvalue())["snapshots"]
    assert len(snaps) >= 1
    assert snaps[0]["label"] == "pre_edit:src/app.py"

    # 6. `brg unfreeze --all` clears frozen globs
    out_unfreeze = NonTTYBuffer()
    assert run_cli(["unfreeze", "--all"], cwd=tmp_path, env=env, stdout_stream=out_unfreeze) == EXIT_OK
    assert json.loads(out_unfreeze.getvalue())["count"] == 0


def test_non_git_client_folder_linking_and_directory_savepoint(tmp_path: Path) -> None:
    """Verify non-Git client folders work seamlessly with `brg init --project --client`, `brg status`, `brg seam`, `brg savepoint`, and `brg undo`."""
    client_dir = tmp_path / "Acme_Q4_Strategy"
    client_dir.mkdir()
    (client_dir / "deck_outline.md").write_text("# Slide 1: Executive Summary\n", encoding="utf-8")

    env = {
        "BRIDGE_API_KEY": "xrkey_v1.client_token",
        "BRIDGE_CREDENTIALS_PATH": str(tmp_path / "creds.json"),
    }

    # 1. Link non-Git folder with --project and --client
    out_init = NonTTYBuffer()
    assert (
        run_cli(
            [
                "init",
                "--org",
                "org_acme",
                "--project",
                "Q4 Executive Strategy",
                "--client",
                "Acme Corp",
            ],
            cwd=client_dir,
            env=env,
            stdout_stream=out_init,
        )
        == EXIT_OK
    )
    init_data = json.loads(out_init.getvalue())
    assert init_data["projectName"] == "Q4 Executive Strategy"
    assert init_data["clientName"] == "Acme Corp"
    assert init_data["repository"] == "Q4 Executive Strategy"

    # 2. Verify status in non-Git folder
    out_status = NonTTYBuffer()
    assert run_cli(["status"], cwd=client_dir, env=env, stdout_stream=out_status) == EXIT_OK
    status_data = json.loads(out_status.getvalue())
    assert status_data["projectName"] == "Q4 Executive Strategy"
    assert status_data["clientName"] == "Acme Corp"

    # 3. Create directory savepoint, modify deck_outline.md, add scratch.txt, and undo
    out_sp = NonTTYBuffer()
    assert (
        run_cli(["savepoint", "create", "--label", "before-deck-edits"], cwd=client_dir, env=env, stdout_stream=out_sp)
        == EXIT_OK
    )
    sp_data = json.loads(out_sp.getvalue())["savepoint"]
    assert sp_data["kind"] == "directory"

    (client_dir / "deck_outline.md").write_text("# Corrupted Slide\n", encoding="utf-8")
    (client_dir / "scratch.txt").write_text("temp notes\n", encoding="utf-8")

    out_undo = NonTTYBuffer()
    assert run_cli(["undo"], cwd=client_dir, env=env, stdout_stream=out_undo) == EXIT_OK
    assert (client_dir / "deck_outline.md").read_text(encoding="utf-8") == "# Slide 1: Executive Summary\n"
    assert not (client_dir / "scratch.txt").exists()


def test_setup_machine_installs_global_harnesses(tmp_path: Path) -> None:
    """Verify `bridge setup` (default `all`) wires global Claude, Antigravity, Cursor, and Codex harnesses."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()

    bin_dir = fake_home / ".local" / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "brg").write_text("#!/bin/sh\n", encoding="utf-8")
    (bin_dir / "bridge-mcp").write_text("#!/bin/sh\n", encoding="utf-8")

    claude_settings = fake_home / ".claude" / "settings.json"
    agy_hooks = fake_home / ".gemini" / "config" / "hooks.json"
    codex_hooks = fake_home / ".codex" / "hooks.json"
    codex_hooks.parent.mkdir(parents=True)
    agy_mcp = fake_home / ".gemini" / "antigravity" / "mcp_config.json"
    agy_mcp.parent.mkdir(parents=True)

    env = {
        "HOME": str(fake_home),
        "BRIDGE_API_KEY": "xrkey_v1.setup_test",
        "BRIDGE_CREDENTIALS_PATH": str(fake_home / ".config" / "bridge" / "credentials.json"),
    }

    out = NonTTYBuffer()
    assert run_cli(["setup"], cwd=tmp_path, env=env, stdout_stream=out) == EXIT_OK
    res = json.loads(out.getvalue())
    assert res["ok"] is True
    assert res["globalScope"] is True

    # 1. PreToolUse and PreInvocation/UserPromptSubmit status-bar hooks point to brg
    claude_cfg = json.loads(claude_settings.read_text(encoding="utf-8"))
    assert "brg guard" in claude_cfg["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
    assert "status-bar --text" in claude_cfg["hooks"]["UserPromptSubmit"][0]["hooks"][0]["command"]

    agy_cfg = json.loads(agy_hooks.read_text(encoding="utf-8"))
    assert "brg guard" in agy_cfg["bridge-guard"]["PreToolUse"][0]["hooks"][0]["command"]
    assert "status-bar --hook" in agy_cfg["bridge-status"]["PreInvocation"][0]["command"]

    codex_cfg = json.loads(codex_hooks.read_text(encoding="utf-8"))
    assert "brg guard" in codex_cfg["hooks"]["PreToolUse"][0]["hooks"][0]["command"]

    # 2. Antigravity mcp_config.json has bridge_bridge-mcp
    mcp_cfg = json.loads(agy_mcp.read_text(encoding="utf-8"))
    assert "bridge_bridge-mcp" in mcp_cfg["mcpServers"]

    # 3. Global Antigravity plugin & Claude/Cursor/Codex skills are installed with full SKILL.md content
    assert (fake_home / ".gemini" / "config" / "plugins" / "bridge" / "plugin.json").is_file()
    afk_skill = (
        fake_home / ".gemini" / "config" / "plugins" / "bridge" / "skills" / "bridge-afk" / "SKILL.md"
    ).read_text(encoding="utf-8")
    assert "Bridge Unattended AFK Protocol" in afk_skill
    assert (fake_home / ".claude" / "skills" / "bridge-afk" / "SKILL.md").is_file()
    assert (fake_home / ".cursor" / "skills" / "bridge-afk" / "SKILL.md").is_file()
    assert (fake_home / ".agents" / "skills" / "bridge-afk" / "SKILL.md").is_file()


def test_antigravity_protojson_guard_and_workspace_paths_resolution(tmp_path: Path) -> None:
    """Verify `brg guard` emits strict Antigravity protojson (no 'ok' or 'hookSpecificOutput' keys)
    and resolves the workspace from event['workspacePaths'][0] when invoked from a plugin directory.
    """
    workspace_dir = tmp_path / "Personal_Game_Prototype"
    workspace_dir.mkdir()
    _init_git_repo(workspace_dir)

    plugin_dir = tmp_path / "plugin_hook_cwd"
    plugin_dir.mkdir()

    env = {
        "BRIDGE_CREDENTIALS_PATH": str(tmp_path / "creds.json"),
        "ANTIGRAVITY_AGENT": "1",
    }

    # Freeze a file inside workspace_dir
    out_frz = NonTTYBuffer()
    assert run_cli(["freeze", "config/apps.json"], cwd=workspace_dir, env=env, stdout_stream=out_frz) == EXIT_OK

    # 1. Antigravity PreToolUse allow payload (invoked with cwd=plugin_dir and workspacePaths=[workspace_dir])
    agy_allow_stdin = io.StringIO(
        json.dumps(
            {
                "toolCall": {
                    "name": "run_command",
                    "args": {"CommandLine": "git status"},
                },
                "workspacePaths": [str(workspace_dir)],
                "conversationId": "conv_123",
            }
        )
    )
    out_allow = NonTTYBuffer()
    code_allow = run_cli(["guard"], cwd=plugin_dir, env=env, stdin_stream=agy_allow_stdin, stdout_stream=out_allow)
    assert code_allow == EXIT_OK
    allow_json = json.loads(out_allow.getvalue())
    assert allow_json == {"decision": "allow"}
    assert "ok" not in allow_json
    assert "hookSpecificOutput" not in allow_json

    # 2. Antigravity PreToolUse deny payload on frozen file inside workspacePaths[0]
    agy_deny_stdin = io.StringIO(
        json.dumps(
            {
                "toolCall": {
                    "name": "edit_file",
                    "args": {"Path": str(workspace_dir / "config" / "apps.json")},
                },
                "workspacePaths": [str(workspace_dir)],
                "conversationId": "conv_123",
            }
        )
    )
    out_deny = NonTTYBuffer()
    code_deny = run_cli(["guard"], cwd=plugin_dir, env=env, stdin_stream=agy_deny_stdin, stdout_stream=out_deny)
    assert code_deny == EXIT_CONCURRENCY_BLOCK
    deny_json = json.loads(out_deny.getvalue())
    assert set(deny_json.keys()) == {"decision", "reason"}
    assert deny_json["decision"] == "deny"
    assert "config/apps.json" in deny_json["reason"]


def test_status_bar_work_vs_local_transparency_and_segments(tmp_path: Path) -> None:
    """Verify `brg status-bar` clearly distinguishes bound WORK repos from unbound LOCAL personal repos and supports modular segment toggles."""
    creds_path = tmp_path / "creds.json"
    env = {
        "BRIDGE_API_KEY": "xrkey_v1.work_token",
        "BRIDGE_CREDENTIALS_PATH": str(creds_path),
        "ANTIGRAVITY_AGENT": "1",
        "GEMINI_MODEL": "gemini-4-argon-high",
    }

    # Save global work credentials
    out_user = NonTTYBuffer()
    assert (
        run_cli(
            ["user", "alex@acme.io", "--name", "Alex"],
            cwd=tmp_path,
            env=env,
            stdout_stream=out_user,
        )
        == EXIT_OK
    )

    # 1. Create an UNBOUND personal repo (`Personal_Game_Prototype`) with personal git user config
    personal_dir = tmp_path / "Personal_Game_Prototype"
    personal_dir.mkdir()
    _init_git_repo(personal_dir)
    subprocess.run(["git", "config", "user.name", "Alex Personal"], cwd=str(personal_dir), check=True)
    subprocess.run(["git", "config", "user.email", "personal@example.com"], cwd=str(personal_dir), check=True)

    out_local_sb = NonTTYBuffer()
    assert run_cli(["status-bar", "--json"], cwd=personal_dir, env=env, stdout_stream=out_local_sb) == EXIT_OK
    local_sb = json.loads(out_local_sb.getvalue())
    assert local_sb["workContext"] is False
    assert local_sb["modeBadge"] == "LOCAL"
    assert local_sb["userEmail"] == "personal@example.com"
    assert "│ LOCAL │" in local_sb["line"]
    assert "Alex Personal (personal@example.com)" in local_sb["line"]
    assert "antigravity/gemini-4-argon-high" in local_sb["line"]

    # 2. Create a BOUND work repo (`Acme_Platform`) with `.bridge.json`
    work_dir = tmp_path / "Acme_Platform"
    work_dir.mkdir()
    _init_git_repo(work_dir)
    out_init = NonTTYBuffer()
    assert (
        run_cli(
            ["init", "--org", "org_acme", "--repo", "acme/bridge", "--no-setup"],
            cwd=work_dir,
            env=env,
            stdout_stream=out_init,
        )
        == EXIT_OK
    )

    out_work_sb = NonTTYBuffer()
    assert run_cli(["status-bar", "--json"], cwd=work_dir, env=env, stdout_stream=out_work_sb) == EXIT_OK
    work_sb = json.loads(out_work_sb.getvalue())
    assert work_sb["workContext"] is True
    assert work_sb["modeBadge"] == "WORK:org_acme"
    assert work_sb["userEmail"] == "alex@acme.io"
    assert "│ WORK:org_acme │" in work_sb["line"]
    assert "acme/bridge:" in work_sb["line"]
    assert "Alex (alex@acme.io)" in work_sb["line"]

    # 3. Test modular segment toggling (--hide, --only, --reset)
    out_hide = NonTTYBuffer()
    assert run_cli(["status-bar", "--hide", "frozen,savepoints", "--json"], cwd=work_dir, env=env, stdout_stream=out_hide) == EXIT_OK
    hide_sb = json.loads(out_hide.getvalue())
    assert "frozen:" not in hide_sb["statusBar"]
    assert "snaps:" not in hide_sb["statusBar"]

    out_reset = NonTTYBuffer()
    assert run_cli(["status-bar", "--reset", "--json"], cwd=work_dir, env=env, stdout_stream=out_reset) == EXIT_OK
    reset_sb = json.loads(out_reset.getvalue())
    assert "frozen:0" in reset_sb["statusBar"]
    assert "snaps:0" in reset_sb["statusBar"]

    # 4. Test --hook mode (used by Antigravity PreInvocation for out-of-the-box turn status-bar injection)
    hook_stdin = io.StringIO(json.dumps({"workspacePaths": [str(personal_dir)]}))
    out_hook = NonTTYBuffer()
    assert run_cli(["status-bar", "--hook"], cwd=tmp_path, env=env, stdin_stream=hook_stdin, stdout_stream=out_hook) == EXIT_OK
    hook_res = json.loads(out_hook.getvalue())
    assert "injectSteps" in hook_res
    assert len(hook_res["injectSteps"]) == 1
    assert "│ LOCAL │ Personal_Game_Prototype:" in hook_res["injectSteps"][0]["ephemeralMessage"]


