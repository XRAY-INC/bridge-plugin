"""Local Git & workspace safety: off-branch savepoints, step-by-step undo/rollback, frozen file locks, and PreToolUse guard."""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from ..config import BridgeConfig, load_local_claims
from ..runtime import (
    EXIT_CONCURRENCY_BLOCK,
    EXIT_CONFIG_MISSING,
    EXIT_ERROR,
    EXIT_OK,
    copy_file_safe,
    detect_git_branch,
    detect_git_commit_sha,
    detect_runtime_context,
    emit_output,
    emit_stderr,
    format_table,
    globs_overlap,
)

EXCLUDED_BACKUP_DIRS = {
    ".git",
    ".bridge",
    ".worktrees",
    "node_modules",
    ".next",
    "__pycache__",
    ".pytest_cache",
    ".venv",
    "venv",
    "dist",
    "build",
}

MAX_BACKUP_FILE_BYTES = 5 * 1024 * 1024  # 5 MB per file in non-Git / untracked snapshots
GUARD_SAVEPOINT_BUDGET_SECONDS = 4.0  # Stay safely below 10s PreToolUse hook timeout on slow/SMB mounts
GUARD_FAST_UNTRACKED_THRESHOLD = 25  # Use stat-based digest for auto-guard when untracked count is high

_copy_file_safe = copy_file_safe


# ---------------------------------------------------------------------------
# 1. Frozen File Locks (.bridge/frozen.json)
# ---------------------------------------------------------------------------


def _normalize_rel_path(path_str: str, workspace_root: Path) -> str:
    """Return a clean workspace-relative forward-slash path."""
    raw = str(path_str or "").strip()
    if not raw:
        return ""
    try:
        p = Path(raw)
        if p.is_absolute():
            rel = p.resolve().relative_to(workspace_root.resolve())
            return str(rel).replace("\\", "/")
    except Exception:
        pass
    cleaned = raw.replace("\\", "/")
    while cleaned.startswith("./"):
        cleaned = cleaned[2:]
    return cleaned.lstrip("/")


def load_frozen_globs(config: BridgeConfig) -> list[str]:
    """Load frozen file globs from .bridge/frozen.json."""
    globs: list[str] = []
    if config.frozen_path.is_file():
        try:
            data = json.loads(config.frozen_path.read_text(encoding="utf-8"))
            raw_list = data.get("globs", []) if isinstance(data, dict) else []
            for item in raw_list:
                if isinstance(item, str) and item.strip() and item.strip() not in globs:
                    globs.append(item.strip())
        except Exception:
            pass
    return globs


def save_frozen_globs(config: BridgeConfig, globs: Sequence[str]) -> Path:
    """Persist frozen file globs to .bridge/frozen.json."""
    config.bridge_dir.mkdir(parents=True, exist_ok=True)
    unique: list[str] = []
    for item in globs:
        s = str(item).strip()
        if s and s not in unique:
            unique.append(s)
    payload = {
        "globs": unique,
        "updatedAt": datetime.now(timezone.utc).isoformat(),
    }
    config.frozen_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return config.frozen_path


def match_frozen_glob(config: BridgeConfig, target_path: str) -> str | None:
    """Return the matching frozen glob if target_path is frozen, or None."""
    rel = _normalize_rel_path(target_path, config.workspace_root)
    if not rel:
        return None
    for pattern in load_frozen_globs(config):
        if globs_overlap(rel, pattern) or fnmatch.fnmatchcase(rel, pattern):
            return pattern
    return None


def handle_freeze(
    args: Any,
    config: BridgeConfig,
    *,
    force_json: bool = False,
    stdout_stream: Any = None,
) -> int:
    """Freeze file or directory globs (or list frozen globs with --list)."""
    existing = load_frozen_globs(config)
    raw_globs = getattr(args, "globs", None) or []
    list_only = bool(getattr(args, "list", False)) or not raw_globs

    if list_only:
        payload = {"ok": True, "frozenGlobs": existing, "count": len(existing)}
        if existing:
            rows = [[idx + 1, g] for idx, g in enumerate(existing)]
            human = format_table(["#", "FROZEN_GLOB"], rows)
        else:
            human = "No frozen file globs configured."
        emit_output(payload, human, force_json=force_json, stdout_stream=stdout_stream)
        return EXIT_OK

    added: list[str] = []
    for item in raw_globs:
        for part in str(item).split(","):
            cleaned = part.strip()
            if cleaned and cleaned not in existing:
                existing.append(cleaned)
                added.append(cleaned)

    save_frozen_globs(config, existing)
    from .statusbar import format_status_bar_line

    sb_line = format_status_bar_line(config)
    payload = {
        "ok": True,
        "added": added,
        "frozenGlobs": existing,
        "count": len(existing),
        "frozenPath": str(config.frozen_path),
        "statusBar": sb_line,
    }
    emit_output(
        payload,
        [f"Frozen {len(existing)} pattern(s): {', '.join(existing)}", *([sb_line] if sb_line else [])],
        force_json=force_json,
        stdout_stream=stdout_stream,
    )
    return EXIT_OK


def handle_unfreeze(
    args: Any,
    config: BridgeConfig,
    *,
    force_json: bool = False,
    stdout_stream: Any = None,
) -> int:
    """Unfreeze specific file/directory globs or --all."""
    existing = load_frozen_globs(config)
    unfreeze_all = bool(getattr(args, "all", False))
    raw_globs = getattr(args, "globs", None) or []

    if not unfreeze_all and not raw_globs:
        emit_output(
            {
                "ok": False,
                "error": "GLOBS_OR_ALL_REQUIRED",
                "message": "Provide globs to unfreeze or pass --all.",
            },
            "Error: Provide globs to unfreeze or pass --all.",
            force_json=force_json,
            stdout_stream=stdout_stream,
        )
        return EXIT_ERROR

    if unfreeze_all:
        removed = list(existing)
        save_frozen_globs(config, [])
        payload = {"ok": True, "removed": removed, "frozenGlobs": [], "count": 0}
        emit_output(
            payload,
            f"Unfroze all {len(removed)} pattern(s).",
            force_json=force_json,
            stdout_stream=stdout_stream,
        )
        return EXIT_OK

    to_remove: set[str] = set()
    for item in raw_globs:
        for part in str(item).split(","):
            if part.strip():
                to_remove.add(part.strip())

    kept = [g for g in existing if g not in to_remove]
    removed = [g for g in existing if g in to_remove]
    save_frozen_globs(config, kept)
    payload = {
        "ok": True,
        "removed": removed,
        "frozenGlobs": kept,
        "count": len(kept),
    }
    emit_output(
        payload,
        f"Unfroze {len(removed)} pattern(s) ({len(kept)} remaining frozen).",
        force_json=force_json,
        stdout_stream=stdout_stream,
    )
    return EXIT_OK


# ---------------------------------------------------------------------------
# 2. Off-Branch Git Savepoints & Non-Git Workspace Snapshots
# ---------------------------------------------------------------------------


def _is_git_repo(root: Path) -> bool:
    if (root / ".git").exists():
        return True
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=str(root),
            capture_output=True,
            text=True,
            errors="replace",
            timeout=3,
            check=False,
        )
        return proc.returncode == 0 and proc.stdout.strip() == "true"
    except Exception:
        return False


def load_snapshots(config: BridgeConfig) -> list[dict[str, Any]]:
    """Load ordered list of savepoints (newest first) from .bridge/snapshots.json."""
    if not config.snapshots_path.is_file():
        return []
    try:
        data = json.loads(config.snapshots_path.read_text(encoding="utf-8"))
        snaps = data.get("snapshots", []) if isinstance(data, dict) else []
        return [s for s in snaps if isinstance(s, dict)]
    except Exception:
        return []


def save_snapshots(config: BridgeConfig, snapshots: list[dict[str, Any]]) -> Path:
    """Persist savepoints list to .bridge/snapshots.json (keeping up to 50 most recent)."""
    config.bridge_dir.mkdir(parents=True, exist_ok=True)
    trimmed = snapshots[:50]
    config.snapshots_path.write_text(
        json.dumps({"snapshots": trimmed}, indent=2) + "\n",
        encoding="utf-8",
    )
    return config.snapshots_path


def _list_untracked_files(root: Path) -> list[str]:
    """Return untracked non-ignored files in a Git repository."""
    try:
        proc = subprocess.run(
            ["git", "ls-files", "--others", "--exclude-standard"],
            cwd=str(root),
            capture_output=True,
            text=True,
            errors="replace",
            timeout=5,
            check=False,
        )
        if proc.returncode != 0:
            return []
        files: list[str] = []
        for line in proc.stdout.splitlines():
            rel = line.strip()
            if not rel:
                continue
            parts = Path(rel).parts
            if any(part in EXCLUDED_BACKUP_DIRS for part in parts):
                continue
            files.append(rel)
        return sorted(files)
    except Exception:
        return []


def _iter_non_git_workspace_files(root: Path) -> list[str]:
    """Return relative paths of files in a non-Git workspace folder."""
    results: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in EXCLUDED_BACKUP_DIRS]
        rel_dir = Path(dirpath).relative_to(root)
        for fn in filenames:
            rel_file = (rel_dir / fn) if str(rel_dir) != "." else Path(fn)
            full_path = root / rel_file
            try:
                if full_path.is_file() and not full_path.is_symlink():
                    if full_path.stat().st_size <= MAX_BACKUP_FILE_BYTES:
                        results.append(str(rel_file).replace("\\", "/"))
            except Exception:
                continue
    return sorted(results)


def _compute_files_digest(
    root: Path,
    rel_paths: Sequence[str],
    *,
    stat_only: bool = False,
    deadline: float | None = None,
) -> str:
    """Compute a deterministic SHA-256 digest over relative file paths and contents (or stat metadata when bounded)."""
    h = hashlib.sha256()
    used_stat_mode = stat_only
    for rel in sorted(rel_paths):
        h.update(rel.encode("utf-8") + b"\0")
        fp = root / rel
        try:
            st = fp.stat()
            if fp.is_file() and st.st_size <= MAX_BACKUP_FILE_BYTES:
                if stat_only or (deadline is not None and time.monotonic() >= deadline):
                    used_stat_mode = True
                    h.update(f"stat:{st.st_size}:{st.st_mtime_ns}".encode("ascii"))
                else:
                    h.update(fp.read_bytes())
        except Exception:
            pass
        h.update(b"\0")
    prefix = "stat:" if used_stat_mode else ""
    return f"{prefix}{h.hexdigest()}"


def create_savepoint_record(
    config: BridgeConfig,
    *,
    label: str = "manual",
    allow_dedup: bool = True,
    auto_guard: bool = False,
) -> dict[str, Any]:
    """Create an off-branch Git savepoint (or directory backup in non-Git folders) and return its metadata."""
    root = config.workspace_root
    now = datetime.now(timezone.utc)
    snap_id = f"snap_{now.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
    existing = load_snapshots(config)
    git_timeout = 4 if auto_guard else 15
    deadline = (time.monotonic() + GUARD_SAVEPOINT_BUDGET_SECONDS) if auto_guard else None

    if _is_git_repo(root):
        branch = detect_git_branch(cwd=root, env=config.env)
        head_sha = detect_git_commit_sha(cwd=root, env=config.env)

        # Capture real index tree SHA first (so we can restore staged vs unstaged state if needed)
        real_tree_proc = subprocess.run(
            ["git", "write-tree"],
            cwd=str(root),
            capture_output=True,
            text=True,
            errors="replace",
            timeout=git_timeout,
            check=False,
        )
        index_tree_sha = real_tree_proc.stdout.strip() if real_tree_proc.returncode == 0 else None

        # Use a temporary GIT_INDEX_FILE to stage all tracked working-tree modifications without touching .git/index
        with tempfile.NamedTemporaryFile(prefix="bridge_idx_", delete=False) as tmp_idx:
            tmp_index_path = Path(tmp_idx.name)
        try:
            idx_proc = subprocess.run(
                ["git", "rev-parse", "--git-path", "index"],
                cwd=str(root),
                capture_output=True,
                text=True,
                errors="replace",
                timeout=git_timeout,
                check=False,
            )
            git_index_orig = (
                (root / idx_proc.stdout.strip())
                if idx_proc.returncode == 0 and idx_proc.stdout.strip()
                else (root / ".git" / "index")
            )
            if git_index_orig.is_file():
                # Ephemeral temp index only needs raw bytes; avoid shutil.copy2 chflags EPERM on macOS SMB mounts.
                shutil.copyfile(git_index_orig, tmp_index_path)
            else:
                tmp_index_path.unlink(missing_ok=True)

            git_env = {**os.environ, "GIT_INDEX_FILE": str(tmp_index_path)}
            subprocess.run(
                ["git", "add", "-u"],
                cwd=str(root),
                env=git_env,
                capture_output=True,
                text=True,
                errors="replace",
                timeout=git_timeout,
                check=False,
            )
            wt_tree_proc = subprocess.run(
                ["git", "write-tree"],
                cwd=str(root),
                env=git_env,
                capture_output=True,
                text=True,
                errors="replace",
                timeout=git_timeout,
                check=False,
            )
            worktree_tree_sha = wt_tree_proc.stdout.strip() if wt_tree_proc.returncode == 0 else ""
        finally:
            tmp_index_path.unlink(missing_ok=True)

        untracked_files = _list_untracked_files(root)
        stat_only_untracked = auto_guard and len(untracked_files) > GUARD_FAST_UNTRACKED_THRESHOLD
        untracked_digest = _compute_files_digest(
            root,
            untracked_files,
            stat_only=stat_only_untracked,
            deadline=deadline,
        )
        fingerprint = f"git:{head_sha or 'none'}:{index_tree_sha or 'none'}:{worktree_tree_sha}:{untracked_digest}"

        if allow_dedup and existing and existing[0].get("fingerprint") == fingerprint:
            return {**existing[0], "deduplicated": True}

        commit_cmd = ["git", "commit-tree", worktree_tree_sha, "-m", f"bridge savepoint {snap_id}: {label}"]
        if head_sha:
            commit_cmd.extend(["-p", head_sha])
        commit_proc = subprocess.run(
            commit_cmd,
            cwd=str(root),
            capture_output=True,
            text=True,
            errors="replace",
            timeout=git_timeout,
            check=False,
        )
        snapshot_commit_sha = commit_proc.stdout.strip() if commit_proc.returncode == 0 else worktree_tree_sha
        if snapshot_commit_sha:
            subprocess.run(
                ["git", "update-ref", f"refs/bridge-snapshots/{snap_id}", snapshot_commit_sha],
                cwd=str(root),
                capture_output=True,
                text=True,
                errors="replace",
                timeout=git_timeout,
                check=False,
            )

        # Backup untracked files into .bridge/snapshots/<snap_id>/untracked/
        untracked_backup_dir = config.bridge_dir / "snapshots" / snap_id / "untracked"
        saved_untracked: list[str] = []
        for rel in untracked_files:
            if deadline is not None and time.monotonic() >= deadline:
                break
            src = root / rel
            try:
                if src.is_file() and src.stat().st_size <= MAX_BACKUP_FILE_BYTES:
                    dest = untracked_backup_dir / rel
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    _copy_file_safe(src, dest)
                    saved_untracked.append(rel)
            except Exception:
                continue

        record: dict[str, Any] = {
            "id": snap_id,
            "kind": "git",
            "label": label,
            "branch": branch,
            "headSha": head_sha,
            "indexTreeSha": index_tree_sha,
            "treeSha": worktree_tree_sha,
            "commitSha": snapshot_commit_sha,
            "ref": f"refs/bridge-snapshots/{snap_id}",
            "untrackedFiles": saved_untracked,
            "fingerprint": fingerprint,
            "createdAt": now.isoformat(),
            "deduplicated": False,
        }
    else:
        rel_files = _iter_non_git_workspace_files(root)
        stat_only_dir = auto_guard and len(rel_files) > GUARD_FAST_UNTRACKED_THRESHOLD
        digest = _compute_files_digest(
            root,
            rel_files,
            stat_only=stat_only_dir,
            deadline=deadline,
        )
        fingerprint = f"dir:{digest}"
        if allow_dedup and existing and existing[0].get("fingerprint") == fingerprint:
            return {**existing[0], "deduplicated": True}

        files_backup_dir = config.bridge_dir / "snapshots" / snap_id / "files"
        saved_files: list[str] = []
        for rel in rel_files:
            if deadline is not None and time.monotonic() >= deadline:
                break
            src = root / rel
            try:
                dest = files_backup_dir / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                _copy_file_safe(src, dest)
                saved_files.append(rel)
            except Exception:
                continue

        record = {
            "id": snap_id,
            "kind": "directory",
            "label": label,
            "branch": "main",
            "headSha": None,
            "indexTreeSha": None,
            "treeSha": None,
            "commitSha": None,
            "ref": None,
            "files": saved_files,
            "untrackedFiles": saved_files,
            "fingerprint": fingerprint,
            "createdAt": now.isoformat(),
            "deduplicated": False,
        }

    existing.insert(0, record)
    save_snapshots(config, existing)
    return record


def _restore_snapshot_record(
    config: BridgeConfig,
    snap: dict[str, Any],
    *,
    dry_run: bool = False,
    force: bool = False,
) -> tuple[int, dict[str, Any], list[str]]:
    """Restore working tree & index to the target savepoint record."""
    root = config.workspace_root
    snap_id = str(snap.get("id") or "")
    kind = str(snap.get("kind") or "git")

    if kind == "git" and _is_git_repo(root):
        current_head = detect_git_commit_sha(cwd=root, env=config.env)
        snap_head = snap.get("headSha")
        if snap_head and current_head and snap_head != current_head and not force:
            payload = {
                "ok": False,
                "error": "HEAD_MOVED_SINCE_SAVEPOINT",
                "snapshotId": snap_id,
                "snapshotHead": snap_head,
                "currentHead": current_head,
                "message": (
                    f"HEAD moved since savepoint '{snap_id}' ({snap_head[:7]} -> {current_head[:7]}). "
                    "Pass --force to restore working tree files across commits."
                ),
            }
            return (
                EXIT_CONFIG_MISSING,
                payload,
                [
                    f"Refused: HEAD moved since savepoint {snap_id} ({snap_head[:7]} -> {current_head[:7]}).",
                    "  Pass --force to restore working tree files anyway.",
                ],
            )

        current_untracked = set(_list_untracked_files(root))
        snap_untracked = set(snap.get("untrackedFiles", []) or [])
        newly_created_untracked = sorted(current_untracked - snap_untracked)

        if dry_run:
            payload = {
                "ok": True,
                "dryRun": True,
                "snapshotId": snap_id,
                "label": snap.get("label"),
                "treeSha": snap.get("treeSha"),
                "rescuedUntrackedFiles": newly_created_untracked,
                "restoredUntrackedFiles": sorted(snap_untracked),
            }
            return (
                EXIT_OK,
                payload,
                [
                    f"[DRY-RUN] Would restore savepoint {snap_id} ({snap.get('label')})",
                    f"  Untracked files to rescue : {len(newly_created_untracked)}",
                    f"  Untracked files to restore: {len(snap_untracked)}",
                ],
            )

        # Capture a pre_undo savepoint first so any undo/rollback is itself reversible
        pre_undo = create_savepoint_record(config, label=f"pre_restore:{snap_id}", allow_dedup=True)

        # Rescue newly created untracked files into .bridge/rescued/<snap_id>/ instead of deleting them
        rescued_dir = config.bridge_dir / "rescued" / snap_id
        rescued_files: list[str] = []
        for rel in newly_created_untracked:
            src = root / rel
            if src.is_file():
                dest = rescued_dir / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(src), str(dest))
                rescued_files.append(rel)

        # Restore tracked files from the snapshot tree
        tree_sha = str(snap.get("treeSha") or snap.get("commitSha") or "")
        if tree_sha:
            subprocess.run(
                ["git", "read-tree", "--reset", "-u", tree_sha],
                cwd=str(root),
                capture_output=True,
                text=True,
                errors="replace",
                timeout=15,
                check=False,
            )
        index_tree = snap.get("indexTreeSha")
        if index_tree:
            subprocess.run(
                ["git", "read-tree", str(index_tree)],
                cwd=str(root),
                capture_output=True,
                text=True,
                errors="replace",
                timeout=15,
                check=False,
            )

        # Restore untracked files that were present at savepoint time
        untracked_backup_dir = config.bridge_dir / "snapshots" / snap_id / "untracked"
        restored_untracked: list[str] = []
        if untracked_backup_dir.is_dir():
            for rel in snap_untracked:
                src = untracked_backup_dir / rel
                if src.is_file():
                    dest = root / rel
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    _copy_file_safe(src, dest)
                    restored_untracked.append(rel)

        payload = {
            "ok": True,
            "restored": True,
            "snapshotId": snap_id,
            "label": snap.get("label"),
            "preRestoreSavepointId": pre_undo.get("id"),
            "rescuedUntrackedFiles": rescued_files,
            "rescuedPath": str(rescued_dir) if rescued_files else None,
            "restoredUntrackedFiles": restored_untracked,
        }
        lines = [
            f"Restored workspace to savepoint {snap_id} ({snap.get('label')}).",
            f"  Pre-restore backup : {pre_undo.get('id')}",
        ]
        if rescued_files:
            lines.append(f"  Rescued {len(rescued_files)} new untracked file(s) -> {rescued_dir}")
        return EXIT_OK, payload, lines

    # Non-Git directory restore
    current_files = set(_iter_non_git_workspace_files(root))
    snap_files = set(snap.get("files", []) or [])
    newly_created = sorted(current_files - snap_files)

    if dry_run:
        payload = {
            "ok": True,
            "dryRun": True,
            "snapshotId": snap_id,
            "label": snap.get("label"),
            "rescuedFiles": newly_created,
            "restoredFiles": sorted(snap_files),
        }
        return (
            EXIT_OK,
            payload,
            [
                f"[DRY-RUN] Would restore directory savepoint {snap_id} ({snap.get('label')})",
                f"  Files to rescue : {len(newly_created)}",
                f"  Files to restore: {len(snap_files)}",
            ],
        )

    pre_undo = create_savepoint_record(config, label=f"pre_restore:{snap_id}", allow_dedup=True)
    rescued_dir = config.bridge_dir / "rescued" / snap_id
    rescued_files = []
    for rel in newly_created:
        src = root / rel
        if src.is_file():
            dest = rescued_dir / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dest))
            rescued_files.append(rel)

    files_backup_dir = config.bridge_dir / "snapshots" / snap_id / "files"
    restored_files: list[str] = []
    if files_backup_dir.is_dir():
        for rel in snap_files:
            src = files_backup_dir / rel
            if src.is_file():
                dest = root / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                _copy_file_safe(src, dest)
                restored_files.append(rel)

    payload = {
        "ok": True,
        "restored": True,
        "snapshotId": snap_id,
        "label": snap.get("label"),
        "preRestoreSavepointId": pre_undo.get("id"),
        "rescuedFiles": rescued_files,
        "restoredFiles": restored_files,
    }
    return (
        EXIT_OK,
        payload,
        [
            f"Restored directory to savepoint {snap_id} ({snap.get('label')}).",
            f"  Pre-restore backup : {pre_undo.get('id')}",
        ],
    )


def handle_savepoint(
    args: Any,
    config: BridgeConfig,
    *,
    force_json: bool = False,
    stdout_stream: Any = None,
) -> int:
    """Create or list local off-branch savepoints."""
    sub = str(getattr(args, "savepoint_action", None) or "create").strip().lower()
    if sub == "list":
        snapshots = load_snapshots(config)
        rows = [
            [
                s.get("id", ""),
                s.get("kind", "git"),
                s.get("branch", ""),
                (s.get("headSha") or "-")[:7],
                s.get("label", ""),
                str(s.get("createdAt", ""))[:19],
            ]
            for s in snapshots
        ]
        payload = {"ok": True, "snapshots": snapshots, "count": len(snapshots)}
        emit_output(
            payload,
            format_table(["ID", "KIND", "BRANCH", "HEAD", "LABEL", "CREATED_AT"], rows),
            force_json=force_json,
            stdout_stream=stdout_stream,
        )
        return EXIT_OK

    label = str(getattr(args, "label", None) or "manual").strip()
    record = create_savepoint_record(config, label=label, allow_dedup=False)
    from .statusbar import format_status_bar_line

    sb_line = format_status_bar_line(config)
    payload = {"ok": True, "savepoint": record, "snapshotId": record["id"], "statusBar": sb_line}
    emit_output(
        payload,
        [
            f"Created savepoint {record['id']} ({record['kind']} · label: '{label}').",
            *([sb_line] if sb_line else []),
        ],
        force_json=force_json,
        stdout_stream=stdout_stream,
    )
    return EXIT_OK


def handle_undo(
    args: Any,
    config: BridgeConfig,
    *,
    force_json: bool = False,
    stdout_stream: Any = None,
) -> int:
    """Rewind working tree & index to the most recent savepoint."""
    snapshots = load_snapshots(config)
    if not snapshots:
        emit_output(
            {
                "ok": False,
                "error": "NO_SAVEPOINTS",
                "message": "No savepoints recorded in this workspace. Run 'brg savepoint create' first.",
            },
            "Error: No savepoints recorded in this workspace.",
            force_json=force_json,
            stdout_stream=stdout_stream,
        )
        return EXIT_ERROR

    target = snapshots[0]
    code, payload, human = _restore_snapshot_record(
        config,
        target,
        dry_run=bool(getattr(args, "dry_run", False)),
        force=bool(getattr(args, "force", False)),
    )
    emit_output(payload, human, force_json=force_json, stdout_stream=stdout_stream)
    return code


def handle_rollback(
    args: Any,
    config: BridgeConfig,
    *,
    force_json: bool = False,
    stdout_stream: Any = None,
) -> int:
    """Restore working tree & index to a specific savepoint ID (or label)."""
    target_ref = str(getattr(args, "snapshot_id", "") or "").strip()
    if not target_ref:
        emit_output(
            {"ok": False, "error": "SNAPSHOT_ID_REQUIRED", "message": "Provide a savepoint ID or label."},
            "Error: Provide a savepoint ID or label.",
            force_json=force_json,
            stdout_stream=stdout_stream,
        )
        return EXIT_ERROR

    snapshots = load_snapshots(config)
    matched = next(
        (
            s
            for s in snapshots
            if s.get("id") == target_ref or s.get("label") == target_ref
        ),
        None,
    )
    if not matched:
        emit_output(
            {
                "ok": False,
                "error": "SAVEPOINT_NOT_FOUND",
                "snapshotId": target_ref,
                "message": f"Savepoint '{target_ref}' not found in .bridge/snapshots.json.",
            },
            f"Error: Savepoint '{target_ref}' not found.",
            force_json=force_json,
            stdout_stream=stdout_stream,
        )
        return EXIT_ERROR

    code, payload, human = _restore_snapshot_record(
        config,
        matched,
        dry_run=bool(getattr(args, "dry_run", False)),
        force=bool(getattr(args, "force", False)),
    )
    emit_output(payload, human, force_json=force_json, stdout_stream=stdout_stream)
    return code


# ---------------------------------------------------------------------------
# 3. PreToolUse Guard (`brg guard`)
# ---------------------------------------------------------------------------

DESTRUCTIVE_BASH_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (
        re.compile(r"(?:^|\s|;|&|\|)rm\s+-[a-zA-Z]*r[a-zA-Z]*f[a-zA-Z]*\s+(?:/|/\*|\~|\~/\*|\$HOME)(?:\s|$)"),
        "Destructive rm -rf on root or home directory is blocked by Bridge Guard.",
    ),
    (
        re.compile(r"(?:^|\s|;|&|\|)git\s+push\s+.*(?:--force(?!-with-lease)|-f(?:\s|$))"),
        "Force-pushing (`git push --force`) is blocked by Bridge Guard. Use `--force-with-lease` on feature branches only if explicitly approved.",
    ),
    (
        re.compile(r"(?:^|\s|;|&|\|)gh\s+pr\s+merge\s+.*--admin"),
        "Bypassing branch protection (`gh pr merge --admin`) is blocked by Bridge Guard.",
    ),
    (
        re.compile(r"(?:^|\s|;|&|\|)git\s+reset\s+--hard"),
        "`git reset --hard` is blocked by Bridge Guard. Use `brg undo` or `brg rollback <snap-id>` to safely rewind without losing untracked work.",
    ),
]

READ_ONLY_BASH_PREFIXES = (
    "git status",
    "git diff",
    "git log",
    "git show",
    "git branch",
    "git rev-parse",
    "git ls-files",
    "git worktree list",
    "brg ",
    "bridge ",
    "ls",
    "pwd",
    "which ",
    "echo ",
    "printf ",
    "cat ",
    "head ",
    "tail ",
    "wc ",
    "rg ",
    "grep ",
    "find ",
    "lsof ",
    "ps ",
    "pytest",
    "uv run pytest",
    "npm test",
    "npx vitest",
    "npx tsc",
)


def _extract_target_paths_from_tool_input(tool_name: str, tool_input: dict[str, Any]) -> list[str]:
    paths: list[str] = []
    for key in ("file_path", "path", "target_file", "TargetFile", "notebook_path", "filePath", "Path"):
        val = tool_input.get(key) or tool_input.get(key.strip())
        if isinstance(val, str) and val.strip():
            paths.append(val.strip())
    edits = tool_input.get("edits")
    if isinstance(edits, list):
        for item in edits:
            if isinstance(item, dict):
                for key in ("file_path", "path", "Path", "TargetFile"):
                    val = item.get(key)
                    if isinstance(val, str) and val.strip():
                        paths.append(val.strip())
    return paths


def _check_other_agent_lease_conflict(
    config: BridgeConfig,
    rel_path: str,
    current_agent: str,
) -> dict[str, Any] | None:
    """Return conflicting lease dict if rel_path is claimed by another agent in .bridge/claims.json."""
    if not rel_path:
        return None
    for claim in load_local_claims(config):
        holder = str(claim.get("agentId") or "").strip()
        if not holder or holder == current_agent:
            continue
        for g in claim.get("claimedGlobs", []) or []:
            if globs_overlap(rel_path, str(g)) or fnmatch.fnmatchcase(rel_path, str(g)):
                return claim
    return None


def _emit_guard_decision(
    *,
    allow: bool,
    reason: str | None = None,
    force_json_cli: bool = False,
    harness_format: str = "claude",
    stdout_stream: Any = None,
) -> int:
    stream = stdout_stream if stdout_stream is not None else sys.stdout
    if harness_format == "antigravity" and not force_json_cli:
        # Strict Antigravity protojson schema: ONLY {"decision": "allow"|"deny", "reason"?: "..."}
        if allow:
            payload: dict[str, Any] = {"decision": "allow"}
            if reason:
                payload["reason"] = reason
            stream.write(json.dumps(payload) + "\n")
            if hasattr(stream, "flush"):
                stream.flush()
            return EXIT_OK
        deny_msg = reason or "Blocked by Bridge PreToolUse Guard."
        payload = {"decision": "deny", "reason": deny_msg}
        stream.write(json.dumps(payload) + "\n")
        if hasattr(stream, "flush"):
            stream.flush()
        return EXIT_CONCURRENCY_BLOCK

    if allow:
        if force_json_cli:
            payload = {
                "ok": True,
                "decision": "approve",
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "allow",
                    **({"permissionDecisionReason": reason} if reason else {}),
                },
            }
            if reason:
                payload["reason"] = reason
        else:
            # Claude Code / Codex PreToolUse hook: emit empty JSON `{}` on allow so Zod validation
            # passes AND Claude Code's normal user permission / auto-mode classifier runs un-bypassed.
            payload = {}
        stream.write(json.dumps(payload) + "\n")
        if hasattr(stream, "flush"):
            stream.flush()
        return EXIT_OK

    deny_msg = reason or "Blocked by Bridge PreToolUse Guard."
    payload = {
        "decision": "block",
        "reason": deny_msg,
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": deny_msg,
        },
    }
    if force_json_cli:
        payload["ok"] = False
    elif stdout_stream is None:
        emit_stderr(deny_msg)
    stream.write(json.dumps(payload) + "\n")
    if hasattr(stream, "flush"):
        stream.flush()
    return EXIT_CONCURRENCY_BLOCK


def handle_guard(
    args: Any,
    config: BridgeConfig,
    *,
    stdin_stream: Any = None,
    stdout_stream: Any = None,
) -> int:
    """Evaluate PreToolUse hook payload from stdin or CLI flags, enforcing frozen files, scope leases, and pre-mutation savepoints."""
    in_stream = stdin_stream if stdin_stream is not None else sys.stdin
    raw_input = ""
    if not getattr(in_stream, "isatty", lambda: False)():
        try:
            raw_input = str(in_stream.read() or "").strip()
        except Exception:
            raw_input = ""

    event: dict[str, Any] = {}
    if raw_input:
        try:
            parsed = json.loads(raw_input)
            if isinstance(parsed, dict):
                event = parsed
        except Exception:
            event = {}

    # Support both Claude Code (tool_name / tool_input) and Antigravity (toolCall.name / toolCall.args)
    tool_call_obj = event.get("toolCall") if isinstance(event.get("toolCall"), dict) else {}
    tool_name = str(
        getattr(args, "tool", None)
        or event.get("tool_name")
        or event.get("toolName")
        or tool_call_obj.get("name")
        or event.get("tool")
        or ""
    ).strip()
    tool_input = (
        event.get("tool_input")
        or event.get("toolInput")
        or tool_call_obj.get("args")
        or tool_call_obj.get("arguments")
        or event.get("input")
        or {}
    )
    if not isinstance(tool_input, dict):
        tool_input = {}

    if getattr(args, "file", None):
        tool_input["file_path"] = str(args.file)
    if getattr(args, "command_str", None):
        tool_input["command"] = str(args.command_str)

    # Re-resolve workspace config when hook runner sets cwd to the hook/plugin directory
    # or operates across multiple workspacePaths (matching the workspace containing target files first).
    candidate_targets = _extract_target_paths_from_tool_input(tool_name, tool_input)
    ws_paths_raw = event.get("workspacePaths") if isinstance(event.get("workspacePaths"), list) else []
    ws_dirs: list[Path] = []
    for wp in ws_paths_raw:
        if isinstance(wp, str) and wp.strip():
            try:
                p_resolved = Path(wp.strip()).resolve()
                if p_resolved.exists():
                    ws_dirs.append(p_resolved)
            except Exception:
                continue

    matched_ws_dir: Path | None = None
    for tp in candidate_targets:
        try:
            tp_path = Path(tp)
            if tp_path.is_absolute():
                tp_resolved = tp_path.resolve()
                for wd in sorted(ws_dirs, key=lambda p: len(str(p)), reverse=True):
                    if tp_resolved == wd or wd in tp_resolved.parents:
                        matched_ws_dir = wd
                        break
            if matched_ws_dir is not None:
                break
        except Exception:
            continue

    event_cwd: Path | None = matched_ws_dir
    if event_cwd is None:
        if isinstance(event.get("cwd"), str) and event["cwd"].strip():
            try:
                c = Path(event["cwd"].strip()).resolve()
                if c.exists():
                    event_cwd = c
            except Exception:
                pass
        elif ws_dirs:
            event_cwd = ws_dirs[0]

    if event_cwd is not None:
        try:
            if event_cwd != config.workspace_root.resolve():
                from ..config import resolve_config

                config = resolve_config(cwd=event_cwd, env=config.env)
        except Exception:
            pass

    force_json_cli = bool(getattr(args, "json", False)) and not bool(raw_input)
    is_antigravity_hook = (
        "toolCall" in event
        or "workspacePaths" in event
        or (
            bool(config.env.get("ANTIGRAVITY_AGENT"))
            and "tool_name" not in event
            and "hook_event_name" not in event
            and not force_json_cli
        )
    )
    harness_format = "antigravity" if is_antigravity_hook else "claude"
    runtime = detect_runtime_context(cwd=config.workspace_root, env=config.env)
    current_agent = runtime["agentId"]

    # 1. File-editing tools (Edit, Write, MultiEdit, NotebookEdit, edit_file, write_file, replace_file_content, etc.)
    edit_tools = {
        "edit",
        "write",
        "multiedit",
        "multi_edit",
        "notebookedit",
        "edit_file",
        "write_file",
        "write_to_file",
        "replace_file_content",
        "multi_replace_file_content",
        "apply_patch",
        "str_replace_editor",
        "mcp_filesystem_write_file",
        "mcp_filesystem_edit_file",
        "mcp_filesystem_move_file",
        "mcp__filesystem__write_file",
        "mcp__filesystem__edit_file",
        "mcp__filesystem__move_file",
    }
    if tool_name.lower() in edit_tools or "file_path" in tool_input or "Path" in tool_input or "TargetFile" in tool_input:
        target_paths = candidate_targets
        for tp in target_paths:
            rel = _normalize_rel_path(tp, config.workspace_root)
            matched_frozen = match_frozen_glob(config, rel)
            if matched_frozen:
                return _emit_guard_decision(
                    allow=False,
                    reason=(
                        f"File '{rel}' matches frozen pattern '{matched_frozen}' (.bridge/frozen.json). "
                        f"Run 'brg unfreeze \"{matched_frozen}\"' if this modification is intentional."
                    ),
                    force_json_cli=force_json_cli,
                    harness_format=harness_format,
                    stdout_stream=stdout_stream,
                )
            conflict = _check_other_agent_lease_conflict(config, rel, current_agent)
            if conflict:
                holder = conflict.get("agentId")
                return _emit_guard_decision(
                    allow=False,
                    reason=(
                        f"File '{rel}' is currently claimed by another agent ('{holder}' on branch "
                        f"'{conflict.get('worktreeBranch', 'main')}'). Coordinate or wait for lease release."
                    ),
                    force_json_cli=force_json_cli,
                    harness_format=harness_format,
                    stdout_stream=stdout_stream,
                )

        # Auto-capture deduplicated pre-mutation savepoint (best-effort: never crash PreToolUse on I/O errors)
        if target_paths:
            rel_first = _normalize_rel_path(target_paths[0], config.workspace_root)
            try:
                create_savepoint_record(
                    config,
                    label=f"pre_edit:{rel_first}",
                    allow_dedup=True,
                    auto_guard=True,
                )
            except Exception as exc:
                emit_stderr(f"Auto-savepoint skipped before edit ({rel_first}): {exc}")
        return _emit_guard_decision(
            allow=True,
            force_json_cli=force_json_cli,
            harness_format=harness_format,
            stdout_stream=stdout_stream,
        )

    # 2. Shell / Bash execution tools
    bash_tools = {"bash", "run_command", "shell", "execute_command"}
    cmd_str = str(
        tool_input.get("command")
        or tool_input.get("CommandLine")
        or tool_input.get("cmd")
        or ""
    ).strip()
    if tool_name.lower() in bash_tools or cmd_str:
        for pattern, reason in DESTRUCTIVE_BASH_PATTERNS:
            if pattern.search(cmd_str):
                return _emit_guard_decision(
                    allow=False,
                    reason=reason,
                    force_json_cli=force_json_cli,
                    harness_format=harness_format,
                    stdout_stream=stdout_stream,
                )

        # Check if command is read-only
        lower_cmd = cmd_str.lower().strip()
        is_read_only = any(lower_cmd.startswith(pref) for pref in READ_ONLY_BASH_PREFIXES)
        if not is_read_only and cmd_str:
            try:
                create_savepoint_record(
                    config,
                    label=f"pre_bash:{cmd_str[:36]}",
                    allow_dedup=True,
                    auto_guard=True,
                )
            except Exception as exc:
                emit_stderr(f"Auto-savepoint skipped before command: {exc}")
        return _emit_guard_decision(
            allow=True,
            force_json_cli=force_json_cli,
            harness_format=harness_format,
            stdout_stream=stdout_stream,
        )

    return _emit_guard_decision(
        allow=True,
        force_json_cli=force_json_cli,
        harness_format=harness_format,
        stdout_stream=stdout_stream,
    )
