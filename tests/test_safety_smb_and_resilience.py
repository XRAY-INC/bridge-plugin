"""Regression tests for SMB/NFS chflags safety, PreToolUse guard resilience, multi-root workspaces, and UTF-8 subprocess decoding."""

from __future__ import annotations

import ast
import errno
import io
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from bridge_plugin.cli import run_cli
from bridge_plugin.commands import safety as safety_mod
from bridge_plugin.config import resolve_config
from bridge_plugin.runtime import (
    EXIT_CONCURRENCY_BLOCK,
    EXIT_OK,
    _run_git,
    copy_file_safe,
)


class NonTTYBuffer(io.StringIO):
    def isatty(self) -> bool:
        return False


def _init_git_repo(repo_dir: Path) -> None:
    repo_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-b", "main"], cwd=str(repo_dir), capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "dev@acme.example"], cwd=str(repo_dir), capture_output=True, check=True)
    subprocess.run(["git", "config", "user.name", "Dev"], cwd=str(repo_dir), capture_output=True, check=True)
    (repo_dir / "README.md").write_text("# Demo Repo\n", encoding="utf-8")
    (repo_dir / "src").mkdir(exist_ok=True)
    (repo_dir / "src" / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=str(repo_dir), capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "initial commit"], cwd=str(repo_dir), capture_output=True, check=True)


def test_copy_file_safe_handles_smb_chflags_permission_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Simulate macOS smbfs SF_ARCHIVED chflags PermissionError inside shutil.copystat."""
    src = tmp_path / "mounted_share" / "notes.txt"
    dst = tmp_path / "local_apfs" / "notes.txt"
    src.parent.mkdir(parents=True)
    dst.parent.mkdir(parents=True)
    src.write_text("archive-flagged payload\n", encoding="utf-8")

    def fail_copystat(_s: Any, _d: Any, *, follow_symlinks: bool = True) -> None:
        raise PermissionError(errno.EPERM, "Operation not permitted")

    monkeypatch.setattr(shutil, "copystat", fail_copystat)

    copy_file_safe(src, dst)
    assert dst.read_text(encoding="utf-8") == "archive-flagged payload\n"


def test_smb_chflags_permission_error_git_savepoint_and_undo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify git savepoint creation, untracked backup, and undo succeed when shutil.copystat raises EPERM."""
    _init_git_repo(tmp_path)
    (tmp_path / "untracked_draft.md").write_text("draft v1\n", encoding="utf-8")
    env = {"BRIDGE_CREDENTIALS_PATH": str(tmp_path / "creds.json")}

    copystat_targets: list[str] = []

    def fail_copystat(src_arg: Any, dst_arg: Any, *, follow_symlinks: bool = True) -> None:
        copystat_targets.append(f"{src_arg} -> {dst_arg}")
        raise PermissionError(errno.EPERM, "Operation not permitted")

    monkeypatch.setattr(shutil, "copystat", fail_copystat)

    # 1. Create savepoint with untracked file while copystat raises PermissionError
    out_sp = NonTTYBuffer()
    assert run_cli(["savepoint", "create", "--label", "smb-baseline"], cwd=tmp_path, env=env, stdout_stream=out_sp) == EXIT_OK
    sp_data = json.loads(out_sp.getvalue())["savepoint"]
    assert "untracked_draft.md" in sp_data["untrackedFiles"]

    # Ephemeral bridge_idx_ temp index must use shutil.copyfile directly (never calling copystat)
    assert not any("bridge_idx_" in entry for entry in copystat_targets)

    # 2. Modify tracked file, modify original untracked file, and add a new untracked file
    (tmp_path / "src" / "app.py").write_text("VALUE = 99\n", encoding="utf-8")
    (tmp_path / "untracked_draft.md").write_text("corrupted draft\n", encoding="utf-8")
    (tmp_path / "extra.tmp").write_text("rescue me\n", encoding="utf-8")

    # 3. Undo must also succeed cleanly under EPERM and restore both tracked and untracked files
    out_undo = NonTTYBuffer()
    assert run_cli(["undo"], cwd=tmp_path, env=env, stdout_stream=out_undo) == EXIT_OK
    undo_payload = json.loads(out_undo.getvalue())
    assert undo_payload["ok"] is True
    assert (tmp_path / "src" / "app.py").read_text(encoding="utf-8") == "VALUE = 1\n"
    assert (tmp_path / "untracked_draft.md").read_text(encoding="utf-8") == "draft v1\n"
    assert not (tmp_path / "extra.tmp").exists()


def test_smb_chflags_permission_error_directory_savepoint_and_undo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify non-Git directory snapshots and undo succeed when shutil.copystat raises EPERM."""
    folder = tmp_path / "NonGit_Share"
    folder.mkdir()
    (folder / "report.md").write_text("v1\n", encoding="utf-8")
    env = {"BRIDGE_CREDENTIALS_PATH": str(tmp_path / "creds.json")}

    def fail_copystat(_s: Any, _d: Any, *, follow_symlinks: bool = True) -> None:
        raise PermissionError(errno.EPERM, "Operation not permitted")

    monkeypatch.setattr(shutil, "copystat", fail_copystat)

    out_sp = NonTTYBuffer()
    assert run_cli(["savepoint", "create", "--label", "dir-smb"], cwd=folder, env=env, stdout_stream=out_sp) == EXIT_OK
    snap = json.loads(out_sp.getvalue())["savepoint"]
    assert snap["kind"] == "directory"
    assert "report.md" in snap["files"]

    (folder / "report.md").write_text("v2\n", encoding="utf-8")
    out_undo = NonTTYBuffer()
    assert run_cli(["undo"], cwd=folder, env=env, stdout_stream=out_undo) == EXIT_OK
    assert (folder / "report.md").read_text(encoding="utf-8") == "v1\n"


def test_guard_savepoint_io_failure_fails_open_while_policy_violations_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ensure unexpected savepoint I/O errors never brick PreToolUse, while frozen/destructive rules still block."""
    _init_git_repo(tmp_path)
    (tmp_path / "locked.cfg").write_text("LOCKED=1\n", encoding="utf-8")
    env = {"BRIDGE_CREDENTIALS_PATH": str(tmp_path / "creds.json")}

    out_frz = NonTTYBuffer()
    assert run_cli(["freeze", "locked.cfg"], cwd=tmp_path, env=env, stdout_stream=out_frz) == EXIT_OK

    def boom_savepoint(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise OSError(errno.EIO, "Simulated network mount I/O glitch")

    monkeypatch.setattr(safety_mod, "create_savepoint_record", boom_savepoint)

    # 1. Safe edit still returns allow (exit 0) even when best-effort savepoint raises OSError
    edit_stdin = io.StringIO(
        json.dumps({"tool_name": "Edit", "tool_input": {"file_path": str(tmp_path / "src" / "app.py")}})
    )
    out_edit = NonTTYBuffer()
    assert run_cli(["guard"], cwd=tmp_path, env=env, stdin_stream=edit_stdin, stdout_stream=out_edit) == EXIT_OK
    assert json.loads(out_edit.getvalue()) == {}

    # 2. Safe mutating bash command also returns allow (exit 0)
    bash_stdin = io.StringIO(
        json.dumps({"tool_name": "Bash", "tool_input": {"command": "touch build_stamp.txt"}})
    )
    out_bash = NonTTYBuffer()
    assert run_cli(["guard"], cwd=tmp_path, env=env, stdin_stream=bash_stdin, stdout_stream=out_bash) == EXIT_OK
    assert json.loads(out_bash.getvalue()) == {}

    # 3. Frozen file edit still fails closed (exit 2)
    frozen_stdin = io.StringIO(
        json.dumps({"tool_name": "Edit", "tool_input": {"file_path": str(tmp_path / "locked.cfg")}})
    )
    out_denied = NonTTYBuffer()
    assert (
        run_cli(["guard"], cwd=tmp_path, env=env, stdin_stream=frozen_stdin, stdout_stream=out_denied)
        == EXIT_CONCURRENCY_BLOCK
    )
    denied_payload = json.loads(out_denied.getvalue())
    assert denied_payload["decision"] == "block"
    assert denied_payload["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_guard_multi_root_workspace_paths_resolves_containing_repo(tmp_path: Path) -> None:
    """Verify multi-root workspacePaths resolves the workspace root containing the edited file."""
    repo_alpha = tmp_path / "repo_alpha"
    repo_beta = tmp_path / "repo_beta"
    _init_git_repo(repo_alpha)
    _init_git_repo(repo_beta)
    (repo_beta / "config").mkdir()
    (repo_beta / "config" / "prod.json").write_text("{}\n", encoding="utf-8")

    env = {"BRIDGE_CREDENTIALS_PATH": str(tmp_path / "creds.json"), "ANTIGRAVITY_AGENT": "1"}
    out_frz = NonTTYBuffer()
    assert run_cli(["freeze", "config/prod.json"], cwd=repo_beta, env=env, stdout_stream=out_frz) == EXIT_OK

    # 1. Frozen file in second workspace root is denied even though repo_alpha is workspacePaths[0]
    deny_stdin = io.StringIO(
        json.dumps(
            {
                "toolCall": {
                    "name": "edit_file",
                    "args": {"Path": str(repo_beta / "config" / "prod.json")},
                },
                "workspacePaths": [str(repo_alpha), str(repo_beta)],
            }
        )
    )
    out_deny = NonTTYBuffer()
    assert (
        run_cli(["guard"], cwd=repo_alpha, env=env, stdin_stream=deny_stdin, stdout_stream=out_deny)
        == EXIT_CONCURRENCY_BLOCK
    )
    assert json.loads(out_deny.getvalue())["decision"] == "deny"

    # 2. Allowed edit in repo_beta records its pre_edit savepoint in repo_beta (not repo_alpha)
    allow_stdin = io.StringIO(
        json.dumps(
            {
                "toolCall": {
                    "name": "edit_file",
                    "args": {"Path": str(repo_beta / "src" / "app.py")},
                },
                "workspacePaths": [str(repo_alpha), str(repo_beta)],
            }
        )
    )
    out_allow = NonTTYBuffer()
    assert run_cli(["guard"], cwd=repo_alpha, env=env, stdin_stream=allow_stdin, stdout_stream=out_allow) == EXIT_OK
    assert json.loads(out_allow.getvalue()) == {"decision": "allow"}
    assert resolve_config(cwd=repo_beta, env=env).snapshots_path.is_file()
    assert not resolve_config(cwd=repo_alpha, env=env).snapshots_path.exists()
    assert not (repo_beta / ".bridge" / "snapshots").exists()
    assert not (repo_beta / ".bridge" / "snapshots.json").exists()


def test_auto_guard_large_untracked_set_uses_stat_digest_and_deduplicates(tmp_path: Path) -> None:
    """Verify auto_guard switches to fast stat digest when untracked files exceed threshold."""
    _init_git_repo(tmp_path)
    for idx in range(30):
        (tmp_path / f"untracked_{idx:02d}.log").write_text(f"log {idx}\n", encoding="utf-8")
    env = {"BRIDGE_CREDENTIALS_PATH": str(tmp_path / "creds.json")}

    for _ in range(2):
        in_ev = io.StringIO(
            json.dumps({"tool_name": "Edit", "tool_input": {"file_path": str(tmp_path / "src" / "app.py")}})
        )
        out_ev = NonTTYBuffer()
        assert run_cli(["guard"], cwd=tmp_path, env=env, stdin_stream=in_ev, stdout_stream=out_ev) == EXIT_OK

    snaps = json.loads(resolve_config(cwd=tmp_path, env=env).snapshots_path.read_text(encoding="utf-8"))["snapshots"]
    assert len(snaps) == 1
    assert ":stat:" in snaps[0]["fingerprint"]
    assert not (tmp_path / ".bridge").exists()


def test_subprocess_utf8_errors_replace_and_zero_raw_copy2_across_codebase(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify non-UTF-8 git stdout never raises UnicodeDecodeError and AST-audit all subprocess.run/copy2 calls."""
    real_run = subprocess.run

    def fake_non_utf8_run(cmd: Any, *args: Any, **kwargs: Any) -> subprocess.CompletedProcess[Any]:
        assert kwargs.get("errors") == "replace"
        # Simulate decoded replacement output for invalid UTF-8 bytes (0xff 0xfe)
        return subprocess.CompletedProcess(cmd, 0, stdout="feat/binary-\ufffd-branch\n", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_non_utf8_run)
    assert _run_git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=tmp_path) == "feat/binary-\ufffd-branch"
    monkeypatch.setattr(subprocess, "run", real_run)

    src_root = Path(__file__).resolve().parents[1] / "src" / "bridge_plugin"
    py_files = sorted(src_root.rglob("*.py"))
    assert len(py_files) >= 12

    for py_path in py_files:
        tree = ast.parse(py_path.read_text(encoding="utf-8"), filename=str(py_path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            # Ensure zero raw shutil.copy2 calls remain in src/bridge_plugin
            if (
                isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "shutil"
                and node.func.attr == "copy2"
            ):
                raise AssertionError(f"Forbidden raw shutil.copy2 in {py_path}:{node.lineno}; use copy_file_safe")

            # Ensure every subprocess.run(..., text=True) sets errors="replace" with no duplicate kwargs
            if (
                isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "subprocess"
                and node.func.attr == "run"
            ):
                kw_names = [kw.arg for kw in node.keywords if kw.arg is not None]
                assert len(kw_names) == len(set(kw_names)), f"Duplicate kwargs in {py_path}:{node.lineno}"
                kw_map = {kw.arg: kw.value for kw in node.keywords if kw.arg is not None}
                if "text" in kw_map and isinstance(kw_map["text"], ast.Constant) and kw_map["text"].value is True:
                    err_val = kw_map.get("errors")
                    assert (
                        isinstance(err_val, ast.Constant) and err_val.value == "replace"
                    ), f"Missing errors='replace' on subprocess.run(..., text=True) in {py_path}:{node.lineno}"


def _rmtree(path: Path) -> None:
    if not path.exists() and not path.is_symlink():
        return
    if path.is_symlink() or path.is_file():
        path.unlink()
        return
    for child in list(path.iterdir()):
        _rmtree(child)
    path.rmdir()


def test_git_savepoint_stores_untracked_in_git_tree_and_undoes_bytes(tmp_path: Path) -> None:
    """Verify git savepoints store untracked files in git objects with zero .bridge/snapshots bloat."""
    _init_git_repo(tmp_path)
    secret_bytes = b"\x00\x01\xffbinary-and-utf8-\xc3\xa9\n"
    untracked_file = tmp_path / "assets" / "payload.bin"
    untracked_file.parent.mkdir(parents=True, exist_ok=True)
    untracked_file.write_bytes(secret_bytes)

    env = {"BRIDGE_CREDENTIALS_PATH": str(tmp_path / "creds.json")}
    out_create = NonTTYBuffer()
    assert run_cli(["savepoint", "create", "--label", "pre-edit", "--json"], cwd=tmp_path, env=env, stdout_stream=out_create) == EXIT_OK
    created = json.loads(out_create.getvalue())
    assert created["savepoint"]["id"]
    assert created["savepoint"].get("untrackedTreeSha")
    assert not (tmp_path / ".bridge" / "snapshots").exists()
    assert not (tmp_path / ".bridge" / "snapshots.json").exists()
    assert resolve_config(cwd=tmp_path, env=env).snapshots_path.is_file()

    # Clobber untracked file, then undo and verify byte-for-byte restore
    untracked_file.write_bytes(b"corrupted")
    out_undo = NonTTYBuffer()
    assert run_cli(["undo", "--json"], cwd=tmp_path, env=env, stdout_stream=out_undo) == EXIT_OK
    assert untracked_file.read_bytes() == secret_bytes


def test_retention_eviction_prunes_git_refs_and_deleted_worktree_orphans(tmp_path: Path) -> None:
    """Verify BRIDGE_MAX_SNAPSHOTS evicts old refs and prune sweeps deleted-worktree orphan refs."""
    _init_git_repo(tmp_path)
    wt_dir = tmp_path / ".worktrees" / "ephemeral-wt"
    wt_dir.parent.mkdir(parents=True, exist_ok=True)
    assert _run_git(["worktree", "add", "-b", "wt-ephemeral", str(wt_dir)], cwd=tmp_path) is not None

    # Create a snapshot inside the linked worktree, then delete the worktree dir directly
    wt_env = {"BRIDGE_CREDENTIALS_PATH": str(tmp_path / "creds.json"), "BRIDGE_MAX_SNAPSHOTS": "3"}
    (wt_dir / "wt_note.txt").write_text("from deleted worktree\n", encoding="utf-8")
    out_wt = NonTTYBuffer()
    assert run_cli(["savepoint", "create", "--label", "wt-snap", "--json"], cwd=wt_dir, env=wt_env, stdout_stream=out_wt) == EXIT_OK
    wt_snap_id = json.loads(out_wt.getvalue())["savepoint"]["id"]
    assert _run_git(["rev-parse", "--verify", f"refs/bridge-snapshots/{wt_snap_id}"], cwd=tmp_path) is not None
    _rmtree(wt_dir)

    # Create 5 snapshots on main repo with max=3; oldest main refs + deleted worktree ref should be pruned
    created_ids: list[str] = []
    for idx in range(5):
        (tmp_path / "src" / "app.py").write_text(f"print({idx})\n", encoding="utf-8")
        out_snap = NonTTYBuffer()
        assert run_cli(["savepoint", "create", "--label", f"s-{idx}", "--json"], cwd=tmp_path, env=wt_env, stdout_stream=out_snap) == EXIT_OK
        created_ids.append(json.loads(out_snap.getvalue())["savepoint"]["id"])

    refs_out = _run_git(["for-each-ref", "--format=%(refname)", "refs/bridge-snapshots"], cwd=tmp_path)
    assert refs_out is not None
    remaining_refs = [line.strip() for line in refs_out.splitlines() if line.strip()]
    for evicted_id in created_ids[:2]:
        assert f"refs/bridge-snapshots/{evicted_id}" not in remaining_refs
    for kept_id in created_ids[2:]:
        assert f"refs/bridge-snapshots/{kept_id}" in remaining_refs
    assert f"refs/bridge-snapshots/{wt_snap_id}" not in remaining_refs


def test_legacy_working_tree_snapshots_auto_migrate_and_undo(tmp_path: Path) -> None:
    """Verify legacy .bridge/snapshots/<id>/untracked/ + snapshots.json auto-migrate and stay undoable."""
    _init_git_repo(tmp_path)
    legacy_id = "legacy-20261114-0001"
    legacy_dir = tmp_path / ".bridge" / "snapshots" / legacy_id / "untracked" / "notes"
    legacy_dir.mkdir(parents=True, exist_ok=True)
    legacy_file = legacy_dir / "draft.txt"
    legacy_file.write_text("precious legacy draft\n", encoding="utf-8")

    # Also copy tracked file so git snapshot is structurally realistic
    head_sha = _run_git(["rev-parse", "HEAD"], cwd=tmp_path)
    tree_sha = _run_git(["rev-parse", "HEAD^{tree}"], cwd=tmp_path)
    assert head_sha and tree_sha
    tracked_copy = tmp_path / ".bridge" / "snapshots" / legacy_id / "tracked" / "src"
    tracked_copy.mkdir(parents=True, exist_ok=True)
    (tracked_copy / "app.py").write_text("print('legacy')\n", encoding="utf-8")

    legacy_index = {
        "snapshots": [
            {
                "id": legacy_id,
                "label": "legacy-pre-edit",
                "kind": "git",
                "branch": "main",
                "headSha": head_sha,
                "treeSha": tree_sha,
                "indexTreeSha": tree_sha,
                "createdAt": "2026-11-14T10:00:00+00:00",
                "autoGuard": False,
                "untrackedFiles": ["notes/draft.txt"],
            }
        ]
    }
    (tmp_path / ".bridge" / "snapshots.json").write_text(json.dumps(legacy_index), encoding="utf-8")

    env = {"BRIDGE_CREDENTIALS_PATH": str(tmp_path / "creds.json")}
    # Listing snapshots triggers migration & cleanup of legacy .bridge/snapshots*
    out_list = NonTTYBuffer()
    assert run_cli(["savepoint", "list", "--json"], cwd=tmp_path, env=env, stdout_stream=out_list) == EXIT_OK
    listed = json.loads(out_list.getvalue())["snapshots"]
    assert len(listed) == 1
    assert listed[0]["id"] == legacy_id
    assert listed[0].get("untrackedTreeSha")
    assert not (tmp_path / ".bridge" / "snapshots").exists()
    assert not (tmp_path / ".bridge" / "snapshots.json").exists()

    # Overwrite migrated untracked file in working tree, then verify rollback restores it
    (tmp_path / "notes").mkdir(parents=True, exist_ok=True)
    (tmp_path / "notes" / "draft.txt").write_text("overwritten\n", encoding="utf-8")
    assert run_cli(["rollback", legacy_id, "--json"], cwd=tmp_path, env=env, stdout_stream=NonTTYBuffer()) == EXIT_OK
    assert (tmp_path / "notes" / "draft.txt").read_text(encoding="utf-8") == "precious legacy draft\n"


def test_guard_skips_unbound_non_git_out_of_workspace_and_readonly_bash(tmp_path: Path) -> None:
    """Verify guard skips auto-savepoint for unbound non-git dirs, outside edits, and read-only shell commands."""
    env = {"BRIDGE_CREDENTIALS_PATH": str(tmp_path / "creds.json")}

    # 1. Unbound non-git folder should not create cache snapshots on Edit
    unbound_dir = tmp_path / "unbound_notes"
    unbound_dir.mkdir(parents=True, exist_ok=True)
    (unbound_dir / "todo.md").write_text("- item\n", encoding="utf-8")
    in_unbound = io.StringIO(
        json.dumps({"tool_name": "Edit", "tool_input": {"file_path": str(unbound_dir / "todo.md")}})
    )
    assert run_cli(["guard"], cwd=unbound_dir, env=env, stdin_stream=in_unbound, stdout_stream=NonTTYBuffer()) == EXIT_OK
    assert not resolve_config(cwd=unbound_dir, env=env).snapshots_path.exists()
    assert not (unbound_dir / ".bridge").exists()

    # Setup bound/git repo for out-of-workspace and bash command checks
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    outside_file = tmp_path / "outside.txt"
    outside_file.write_text("outside\n", encoding="utf-8")

    # 2. Out-of-workspace file edit should not create a repo savepoint
    in_outside = io.StringIO(
        json.dumps({"tool_name": "Write", "tool_input": {"file_path": str(outside_file)}})
    )
    assert run_cli(["guard"], cwd=repo, env=env, stdin_stream=in_outside, stdout_stream=NonTTYBuffer()) == EXIT_OK
    assert not resolve_config(cwd=repo, env=env).snapshots_path.exists()

    # 3. Read-only Bash commands should not create savepoints
    for ro_cmd in [
        "npx prettier --check .",
        "npx eslint .",
        "npx jest",
        "gh pr view 1324",
        f"git -C {repo} status",
    ]:
        in_ro = io.StringIO(json.dumps({"tool_name": "Bash", "tool_input": {"command": ro_cmd}}))
        assert run_cli(["guard"], cwd=repo, env=env, stdin_stream=in_ro, stdout_stream=NonTTYBuffer()) == EXIT_OK
    assert not resolve_config(cwd=repo, env=env).snapshots_path.exists()

    # 4. Mutating Bash commands SHOULD create auto-guard savepoints
    for idx, mut_cmd in enumerate(["npx eslint --fix .", "echo x > out.txt"], start=1):
        (repo / "src" / "app.py").write_text(f"print({idx})\n", encoding="utf-8")
        in_mut = io.StringIO(json.dumps({"tool_name": "Bash", "tool_input": {"command": mut_cmd}}))
        assert run_cli(["guard"], cwd=repo, env=env, stdin_stream=in_mut, stdout_stream=NonTTYBuffer()) == EXIT_OK

    snaps = json.loads(resolve_config(cwd=repo, env=env).snapshots_path.read_text(encoding="utf-8"))["snapshots"]
    assert len(snaps) == 2


def test_savepoint_prune_keep_all_and_scan(tmp_path: Path) -> None:
    """Verify savepoint prune supports --keep, --all, and --scan across workspaces."""
    parent = tmp_path / "projects"
    repo_a = parent / "alpha"
    repo_b = parent / "beta"
    _init_git_repo(repo_a)
    _init_git_repo(repo_b)
    env = {"BRIDGE_CREDENTIALS_PATH": str(tmp_path / "creds.json")}

    for idx in range(5):
        (repo_a / "src" / "app.py").write_text(f"a={idx}\n", encoding="utf-8")
        assert run_cli(["savepoint", "create", "--label", f"a-{idx}", "--json"], cwd=repo_a, env=env, stdout_stream=NonTTYBuffer()) == EXIT_OK

    for idx in range(2):
        (repo_b / "src" / "app.py").write_text(f"b={idx}\n", encoding="utf-8")
        assert run_cli(["savepoint", "create", "--label", f"b-{idx}", "--json"], cwd=repo_b, env=env, stdout_stream=NonTTYBuffer()) == EXIT_OK

    # Inject a legacy .bridge/snapshots folder in repo_b to ensure --scan cleans it up too
    legacy_dir = repo_b / ".bridge" / "snapshots" / "old-snap" / "untracked"
    legacy_dir.mkdir(parents=True, exist_ok=True)
    (legacy_dir / "junk.log").write_text("junk\n", encoding="utf-8")

    # Prune repo_a directly to keep 3
    out_keep = NonTTYBuffer()
    assert run_cli(["savepoint", "prune", "--keep", "3", "--json"], cwd=repo_a, env=env, stdout_stream=out_keep) == EXIT_OK
    snaps_a = json.loads(resolve_config(cwd=repo_a, env=env).snapshots_path.read_text(encoding="utf-8"))["snapshots"]
    assert len(snaps_a) == 3

    # Scan parent with --keep 1 and check both repos were pruned and legacy dir removed
    out_scan = NonTTYBuffer()
    assert run_cli(["savepoint", "prune", "--keep", "1", "--scan", str(parent), "--json"], cwd=tmp_path, env=env, stdout_stream=out_scan) == EXIT_OK
    assert len(json.loads(resolve_config(cwd=repo_a, env=env).snapshots_path.read_text(encoding="utf-8"))["snapshots"]) == 1
    assert len(json.loads(resolve_config(cwd=repo_b, env=env).snapshots_path.read_text(encoding="utf-8"))["snapshots"]) == 1
    assert not (repo_b / ".bridge" / "snapshots").exists()

    # Prune --all on repo_a clears all remaining snapshots and git refs
    out_all = NonTTYBuffer()
    assert run_cli(["savepoint", "prune", "--all", "--json"], cwd=repo_a, env=env, stdout_stream=out_all) == EXIT_OK
    assert not resolve_config(cwd=repo_a, env=env).snapshots_path.exists()
    assert safety_mod.load_snapshots(resolve_config(cwd=repo_a, env=env)) == []
    assert _run_git(["for-each-ref", "--format=%(refname)", "refs/bridge-snapshots"], cwd=repo_a) == ""
