"""Local Git & workspace safety: off-branch savepoints, step-by-step undo/rollback, frozen file locks, and PreToolUse guard."""

from __future__ import annotations

import fnmatch
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from ..config import BridgeConfig, load_local_claims, resolve_config
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
    ".turbo",
    ".vercel",
    ".output",
    ".parcel-cache",
    ".cache",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".venv",
    "venv",
    "dist",
    "build",
    "out",
    "target",
    "coverage",
    "playwright-report",
    "test-results",
    "vendor",
    ".agents",
    ".agent",
    ".claude",
    ".cursor",
    ".gemini",
}

MAX_BACKUP_FILE_BYTES = 5 * 1024 * 1024  # 5 MB per file in non-Git / untracked snapshots
GUARD_SAVEPOINT_BUDGET_SECONDS = 4.0  # Stay safely below 10s PreToolUse hook timeout on slow/SMB mounts
GUARD_FAST_UNTRACKED_THRESHOLD = 25  # Use stat-based digest for auto-guard when untracked count is high
DEFAULT_MAX_SNAPSHOTS = 25
AUTO_SNAPSHOT_TTL_SECONDS = 7 * 24 * 60 * 60  # 7 days for auto-guard savepoints
MAX_AUTO_UNTRACKED_TOTAL_BYTES = 50 * 1024 * 1024  # 50 MB per auto-guard snapshot
MAX_DIR_CACHE_BYTES = 250 * 1024 * 1024  # 250 MB per non-Git workspace blob cache
MAX_RESCUED_DIRS = 10

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


def _parse_iso_epoch(raw: Any) -> float | None:
    text = str(raw or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _get_max_snapshots(config: BridgeConfig) -> int:
    raw = str(config.env.get("BRIDGE_MAX_SNAPSHOTS", "") or "").strip()
    if raw:
        try:
            return max(1, int(raw))
        except ValueError:
            pass
    return DEFAULT_MAX_SNAPSHOTS


def _is_auto_guard_record(snap: dict[str, Any]) -> bool:
    if snap.get("autoGuard") is True:
        return True
    label = str(snap.get("label") or "")
    return label.startswith(("pre_edit:", "pre_bash:"))


def _filter_retained_snapshots(
    snapshots: Sequence[dict[str, Any]],
    *,
    max_keep: int | None,
    now_epoch: float | None = None,
) -> list[dict[str, Any]]:
    """Apply age TTL for auto_guard snapshots (7 days) and count cap (newest first)."""
    if max_keep is not None and max_keep <= 0:
        return []
    now = time.time() if now_epoch is None else now_epoch
    cutoff = now - AUTO_SNAPSHOT_TTL_SECONDS
    retained: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for s in snapshots:
        if not isinstance(s, dict):
            continue
        sid = str(s.get("id") or "")
        if sid and sid in seen_ids:
            continue
        if _is_auto_guard_record(s):
            ts = _parse_iso_epoch(s.get("createdAt"))
            if ts is not None and ts < cutoff:
                continue
        if sid:
            seen_ids.add(sid)
        retained.append(s)
        if max_keep is not None and len(retained) >= max_keep:
            break
    return retained


def _dir_size_bytes(path: Path) -> int:
    total = 0
    if not path.exists():
        return 0
    if path.is_file() and not path.is_symlink():
        try:
            return path.lstat().st_size
        except OSError:
            return 0
    for dirpath, _dirnames, filenames in os.walk(path, followlinks=False):
        for fn in filenames:
            fp = Path(dirpath) / fn
            try:
                if not fp.is_symlink():
                    total += fp.lstat().st_size
            except OSError:
                continue
    return total


def _format_bytes_human(num_bytes: int) -> str:
    val = float(max(0, num_bytes))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if val < 1024.0 or unit == "TB":
            return f"{int(val)} B" if unit == "B" else f"{val:.1f} {unit}"
        val /= 1024.0
    return f"{val:.1f} TB"


def _cleanup_empty_bridge_dir(bridge_dir: Path) -> bool:
    """Remove empty .bridge/ (ignoring Finder .DS_Store) so unbound repos stay clean."""
    if not bridge_dir.is_dir():
        return False
    try:
        entries = list(bridge_dir.iterdir())
        if len(entries) == 1 and entries[0].name == ".DS_Store":
            entries[0].unlink(missing_ok=True)
            entries = list(bridge_dir.iterdir())
        if not entries:
            bridge_dir.rmdir()
            return True
    except OSError:
        pass
    return False


def _read_snapshots_json(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        snaps = data.get("snapshots", []) if isinstance(data, dict) else []
        return [s for s in snaps if isinstance(s, dict)]
    except Exception:
        return []


def _write_snapshots_json(path: Path, snapshots: Sequence[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"snapshots": list(snapshots)}, indent=2) + "\n",
        encoding="utf-8",
    )


def _write_git_untracked_tree(
    work_tree: Path,
    rel_paths: Sequence[str],
    *,
    git_timeout: int = 10,
    git_dir: Path | None = None,
) -> str | None:
    """Write rel_paths into a Git tree object via an ephemeral index without touching .git/index or .bridge/."""
    existing_paths = [
        str(p)
        for p in rel_paths
        if str(p).strip() and (work_tree / str(p)).is_file() and not (work_tree / str(p)).is_symlink()
    ]
    if not existing_paths:
        return None
    with tempfile.NamedTemporaryFile(prefix="bridge_ut_idx_", delete=False) as tmp_idx:
        tmp_ut_path = Path(tmp_idx.name)
    # Remove the empty temp file so Git creates a fresh index header cleanly
    tmp_ut_path.unlink(missing_ok=True)
    try:
        ut_env = {**os.environ, "GIT_INDEX_FILE": str(tmp_ut_path)}
        if git_dir is not None:
            ut_env["GIT_DIR"] = str(git_dir)
            ut_env["GIT_WORK_TREE"] = str(work_tree)
        batch_size = 100
        for idx in range(0, len(existing_paths), batch_size):
            batch = existing_paths[idx : idx + batch_size]
            proc_add = subprocess.run(
                ["git", "add", "-f", "--", *batch],
                cwd=str(work_tree),
                env=ut_env,
                capture_output=True,
                text=True,
                errors="replace",
                timeout=git_timeout,
                check=False,
            )
            if proc_add.returncode != 0:
                return None
        proc_tree = subprocess.run(
            ["git", "write-tree"],
            cwd=str(work_tree),
            env=ut_env,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=git_timeout,
            check=False,
        )
        tree_sha = proc_tree.stdout.strip() if proc_tree.returncode == 0 else ""
        return tree_sha or None
    except Exception:
        return None
    finally:
        tmp_ut_path.unlink(missing_ok=True)


def _run_git_commit_tree(
    cwd: Path,
    tree_sha: str,
    message: str,
    *,
    parents: Sequence[str] = (),
    git_dir: Path | None = None,
    timeout: int = 10,
) -> str | None:
    """Run `git commit-tree`, falling back to synthetic committer identity if global git user is unset."""
    if not tree_sha:
        return None
    cmd = ["git", "commit-tree", tree_sha, "-m", message]
    for parent in parents:
        if parent:
            cmd.extend(["-p", parent])
    base_env = dict(os.environ)
    if git_dir is not None:
        base_env["GIT_DIR"] = str(git_dir)
    proc = subprocess.run(
        cmd,
        cwd=str(cwd),
        env=base_env,
        capture_output=True,
        text=True,
        errors="replace",
        timeout=timeout,
        check=False,
    )
    if proc.returncode == 0 and proc.stdout.strip():
        return proc.stdout.strip()
    fallback_env = {
        **base_env,
        "GIT_AUTHOR_NAME": base_env.get("GIT_AUTHOR_NAME") or "Bridge Savepoint",
        "GIT_AUTHOR_EMAIL": base_env.get("GIT_AUTHOR_EMAIL") or "bridge@xray.tech",
        "GIT_COMMITTER_NAME": base_env.get("GIT_COMMITTER_NAME") or "Bridge Savepoint",
        "GIT_COMMITTER_EMAIL": base_env.get("GIT_COMMITTER_EMAIL") or "bridge@xray.tech",
    }
    retry = subprocess.run(
        cmd,
        cwd=str(cwd),
        env=fallback_env,
        capture_output=True,
        text=True,
        errors="replace",
        timeout=timeout,
        check=False,
    )
    if retry.returncode == 0 and retry.stdout.strip():
        return retry.stdout.strip()
    return None


def _pin_git_snapshot_ref(
    root: Path,
    *,
    snap_id: str,
    label: str,
    worktree_tree_sha: str,
    head_sha: str | None,
    index_tree_sha: str | None,
    untracked_tree_sha: str | None,
    git_timeout: int = 10,
    git_dir: Path | None = None,
) -> str:
    """Create snapshot commit keeping worktree, index, and untracked trees reachable, and pin refs/bridge-snapshots/<id>."""
    if not worktree_tree_sha:
        return ""
    parents: list[str] = []
    if head_sha:
        parents.append(head_sha)
    if index_tree_sha and index_tree_sha != worktree_tree_sha:
        idx_commit = _run_git_commit_tree(
            root,
            index_tree_sha,
            f"bridge index {snap_id}",
            git_dir=git_dir,
            timeout=git_timeout,
        )
        if idx_commit:
            parents.append(idx_commit)
    if untracked_tree_sha:
        ut_commit = _run_git_commit_tree(
            root,
            untracked_tree_sha,
            f"bridge untracked {snap_id}",
            git_dir=git_dir,
            timeout=git_timeout,
        )
        if ut_commit:
            parents.append(ut_commit)

    snapshot_commit_sha = (
        _run_git_commit_tree(
            root,
            worktree_tree_sha,
            f"bridge savepoint {snap_id}: {label}",
            parents=parents,
            git_dir=git_dir,
            timeout=git_timeout,
        )
        or worktree_tree_sha
    )
    if snapshot_commit_sha:
        env = dict(os.environ)
        if git_dir is not None:
            env["GIT_DIR"] = str(git_dir)
        subprocess.run(
            ["git", "update-ref", f"refs/bridge-snapshots/{snap_id}", snapshot_commit_sha],
            cwd=str(root),
            env=env,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=git_timeout,
            check=False,
        )
    return snapshot_commit_sha


def _store_dir_blobs(
    source_root: Path,
    rel_paths: Sequence[str],
    blobs_dir: Path,
    *,
    deadline: float | None = None,
    max_total_bytes: int | None = None,
) -> tuple[list[str], dict[str, str]]:
    """Store files into content-addressed SHA-256 blobs under blobs_dir (deduplicated across savepoints)."""
    saved_files: list[str] = []
    file_blobs: dict[str, str] = {}
    total_bytes = 0
    for rel in rel_paths:
        if deadline is not None and time.monotonic() >= deadline:
            break
        src = source_root / rel
        try:
            if not src.is_file() or src.is_symlink():
                continue
            st_size = src.stat().st_size
            if st_size > MAX_BACKUP_FILE_BYTES:
                continue
            if max_total_bytes is not None and saved_files and (total_bytes + st_size > max_total_bytes):
                break
            data = src.read_bytes()
            sha = hashlib.sha256(data).hexdigest()
            blob_path = blobs_dir / sha[:2] / sha
            if not blob_path.is_file():
                blob_path.parent.mkdir(parents=True, exist_ok=True)
                blob_path.write_bytes(data)
            saved_files.append(rel)
            file_blobs[rel] = sha
            total_bytes += len(data)
        except Exception:
            continue
    return saved_files, file_blobs


def _collect_active_git_snapshot_refs(
    config: BridgeConfig,
    retained: Sequence[dict[str, Any]],
) -> set[str]:
    """Collect refs pinned by the current workspace plus any still-existing sibling linked worktrees."""
    active: set[str] = set()
    for s in retained:
        ref = s.get("ref") or (f"refs/bridge-snapshots/{s.get('id')}" if s.get("id") else None)
        if isinstance(ref, str) and ref.startswith("refs/bridge-snapshots/"):
            active.add(ref)

    common_dir = config.git_common_dir
    git_dir = config.git_dir
    if common_dir is None:
        return active

    # If invoked inside a linked worktree, protect the primary checkout's active savepoints
    if git_dir is not None and common_dir.resolve() != git_dir.resolve():
        primary_paths = [common_dir / "bridge" / "snapshots.json"]
        if common_dir.name == ".git":
            primary_paths.append(common_dir.parent / ".bridge" / "snapshots.json")
        for p in primary_paths:
            for s in _read_snapshots_json(p):
                ref = s.get("ref") or (f"refs/bridge-snapshots/{s.get('id')}" if s.get("id") else None)
                if isinstance(ref, str) and ref.startswith("refs/bridge-snapshots/"):
                    active.add(ref)

    worktrees_meta = common_dir / "worktrees"
    if worktrees_meta.is_dir():
        for wt_meta_dir in worktrees_meta.iterdir():
            if not wt_meta_dir.is_dir():
                continue
            if git_dir is not None and wt_meta_dir.resolve() == git_dir.resolve():
                continue
            wt_gitdir_file = wt_meta_dir / "gitdir"
            wt_alive = False
            wt_root: Path | None = None
            if wt_gitdir_file.is_file():
                try:
                    raw_wt_gd = wt_gitdir_file.read_text(encoding="utf-8", errors="replace").strip()
                    if raw_wt_gd:
                        wt_dot_git = Path(raw_wt_gd)
                        if wt_dot_git.exists():
                            wt_alive = True
                            wt_root = wt_dot_git.parent
                except OSError:
                    wt_alive = False
            if wt_alive:
                wt_manifest_paths = [wt_meta_dir / "bridge" / "snapshots.json"]
                if wt_root is not None:
                    wt_manifest_paths.append(wt_root / ".bridge" / "snapshots.json")
                for p in wt_manifest_paths:
                    for s in _read_snapshots_json(p):
                        ref = s.get("ref") or (f"refs/bridge-snapshots/{s.get('id')}" if s.get("id") else None)
                        if isinstance(ref, str) and ref.startswith("refs/bridge-snapshots/"):
                            active.add(ref)
            else:
                stale_bridge = wt_meta_dir / "bridge"
                if stale_bridge.is_dir():
                    shutil.rmtree(stale_bridge, ignore_errors=True)
    return active


def _prune_rescued_dirs(bridge_dir: Path, *, max_keep: int = MAX_RESCUED_DIRS) -> tuple[int, int]:
    """Keep at most max_keep newest .bridge/rescued/<snap_id>/ folders."""
    rescued_root = bridge_dir / "rescued"
    if not rescued_root.is_dir():
        return (0, 0)
    try:
        entries = [p for p in rescued_root.iterdir() if p.is_dir()]
        entries.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return (0, 0)
    deleted = 0
    reclaimed = 0
    for stale in entries[max_keep:]:
        reclaimed += _dir_size_bytes(stale)
        shutil.rmtree(stale, ignore_errors=True)
        deleted += 1
    try:
        if not list(rescued_root.iterdir()):
            rescued_root.rmdir()
            _cleanup_empty_bridge_dir(bridge_dir)
    except OSError:
        pass
    return (deleted, reclaimed)


def _migrate_legacy_snapshots_if_needed(
    config: BridgeConfig,
    *,
    run_prune: bool = True,
) -> dict[str, int]:
    """Migrate pre-0.7.5 <repo>/.bridge/snapshots* into .git/bridge or ~/.cache/bridge and delete unpacked bloat."""
    leg_json = config.legacy_snapshots_path
    leg_dir = config.legacy_snapshots_dir
    if not leg_json.is_file() and not leg_dir.is_dir():
        return {"reclaimedBytes": 0, "deletedLegacyDirs": 0}

    reclaimed_bytes = _dir_size_bytes(leg_dir)
    if leg_json.is_file():
        try:
            reclaimed_bytes += leg_json.lstat().st_size
        except OSError:
            pass

    deleted_legacy_dirs = 0
    if leg_dir.is_dir():
        try:
            subdirs = [p for p in leg_dir.iterdir() if p.is_dir()]
            deleted_legacy_dirs = len(subdirs) if subdirs else 1
        except OSError:
            deleted_legacy_dirs = 1

    new_snaps = _read_snapshots_json(config.snapshots_path)
    old_snaps = _read_snapshots_json(leg_json)
    merged = _filter_retained_snapshots([*new_snaps, *old_snaps], max_keep=_get_max_snapshots(config))
    gd = config.git_dir

    migrated: list[dict[str, Any]] = []
    for raw in merged:
        snap = dict(raw)
        sid = str(snap.get("id") or "")
        kind = str(snap.get("kind") or "git")
        if kind == "git" and gd is not None and not snap.get("untrackedTreeSha") and sid:
            leg_ut = leg_dir / sid / "untracked"
            if leg_ut.is_dir():
                ut_list = list(snap.get("untrackedFiles") or [])
                if not ut_list:
                    ut_list = _iter_non_git_workspace_files(leg_ut)
                ut_sha = _write_git_untracked_tree(leg_ut, ut_list, git_timeout=10, git_dir=gd)
                if ut_sha:
                    snap["untrackedTreeSha"] = ut_sha
                    wt_sha = str(snap.get("treeSha") or "")
                    if wt_sha:
                        commit_sha = _pin_git_snapshot_ref(
                            config.workspace_root,
                            snap_id=sid,
                            label=str(snap.get("label") or "migrated"),
                            worktree_tree_sha=wt_sha,
                            head_sha=snap.get("headSha"),
                            index_tree_sha=snap.get("indexTreeSha"),
                            untracked_tree_sha=ut_sha,
                            git_timeout=10,
                            git_dir=gd,
                        )
                        if commit_sha:
                            snap["commitSha"] = commit_sha
        elif kind == "directory" and not snap.get("fileBlobs") and sid:
            leg_files = leg_dir / sid / "files"
            if leg_files.is_dir():
                rel_list = list(snap.get("files") or [])
                if not rel_list:
                    rel_list = _iter_non_git_workspace_files(leg_files)
                saved_files, file_blobs = _store_dir_blobs(
                    leg_files,
                    rel_list,
                    config.snapshots_dir / "blobs",
                )
                snap["files"] = saved_files
                snap["untrackedFiles"] = saved_files
                snap["fileBlobs"] = file_blobs
        migrated.append(snap)

    if migrated or config.snapshots_path.is_file():
        _write_snapshots_json(config.snapshots_path, migrated)

    leg_json.unlink(missing_ok=True)
    if leg_dir.is_dir():
        shutil.rmtree(leg_dir, ignore_errors=True)
    _cleanup_empty_bridge_dir(config.bridge_dir)

    if run_prune:
        prune_snapshots(config, retained=migrated, allow_gc=False)
    return {
        "reclaimedBytes": reclaimed_bytes,
        "deletedLegacyDirs": deleted_legacy_dirs,
    }


def prune_snapshots(
    config: BridgeConfig,
    *,
    retained: list[dict[str, Any]] | None = None,
    keep: int | None = None,
    aggressive_gc: bool = False,
    allow_gc: bool = True,
) -> dict[str, Any]:
    """Prune evicted Git snapshot refs, unreferenced cache blobs, legacy .bridge/snapshots/, and stale rescued dirs."""
    leg_json = config.legacy_snapshots_path
    leg_dir = config.legacy_snapshots_dir
    legacy_reclaimed = 0
    legacy_dirs_deleted = 0
    deleted_snapshots = 0

    if keep == 0:
        # Full wipe in this workspace: no need to migrate legacy dirs before removing them
        before_count = len(_read_snapshots_json(config.snapshots_path)) + len(_read_snapshots_json(leg_json))
        legacy_reclaimed += _dir_size_bytes(leg_dir)
        if leg_json.is_file():
            try:
                legacy_reclaimed += leg_json.lstat().st_size
            except OSError:
                pass
        if leg_dir.is_dir():
            try:
                subdirs = [p for p in leg_dir.iterdir() if p.is_dir()]
                legacy_dirs_deleted = len(subdirs) if subdirs else 1
            except OSError:
                legacy_dirs_deleted = 1
        leg_json.unlink(missing_ok=True)
        if leg_dir.is_dir():
            shutil.rmtree(leg_dir, ignore_errors=True)
        _cleanup_empty_bridge_dir(config.bridge_dir)
        effective_retained: list[dict[str, Any]] = []
        if config.snapshots_path.is_file():
            config.snapshots_path.unlink(missing_ok=True)
        deleted_snapshots = before_count
    else:
        if leg_json.is_file() or leg_dir.is_dir():
            mig_stats = _migrate_legacy_snapshots_if_needed(config, run_prune=False)
            legacy_reclaimed += mig_stats.get("reclaimedBytes", 0)
            legacy_dirs_deleted += mig_stats.get("deletedLegacyDirs", 0)
        if retained is None:
            raw_snaps = _read_snapshots_json(config.snapshots_path)
            max_keep = keep if keep is not None else _get_max_snapshots(config)
            effective_retained = _filter_retained_snapshots(raw_snaps, max_keep=max_keep)
            deleted_snapshots = max(0, len(raw_snaps) - len(effective_retained))
            if config.snapshots_path.is_file() or effective_retained:
                _write_snapshots_json(config.snapshots_path, effective_retained)
        else:
            effective_retained = list(retained)

    rescued_deleted, rescued_reclaimed = _prune_rescued_dirs(config.bridge_dir)
    deleted_git_refs = 0
    deleted_blobs = 0
    blob_reclaimed = 0

    # Git ref GC (including refs leaked by evicted snapshots or deleted linked worktrees)
    if _is_git_repo(config.workspace_root):
        active_refs = _collect_active_git_snapshot_refs(config, effective_retained)
        try:
            list_proc = subprocess.run(
                ["git", "for-each-ref", "--format=%(refname)", "refs/bridge-snapshots"],
                cwd=str(config.workspace_root),
                capture_output=True,
                text=True,
                errors="replace",
                timeout=5,
                check=False,
            )
            if list_proc.returncode == 0:
                stale_refs = [
                    line.strip()
                    for line in list_proc.stdout.splitlines()
                    if line.strip().startswith("refs/bridge-snapshots/") and line.strip() not in active_refs
                ]
                if stale_refs:
                    for idx in range(0, len(stale_refs), 500):
                        batch = stale_refs[idx : idx + 500]
                        stdin_payload = "".join(f"delete {ref}\n" for ref in batch)
                        del_proc = subprocess.run(
                            ["git", "update-ref", "--stdin"],
                            cwd=str(config.workspace_root),
                            input=stdin_payload,
                            capture_output=True,
                            text=True,
                            errors="replace",
                            timeout=10,
                            check=False,
                        )
                        if del_proc.returncode == 0:
                            deleted_git_refs += len(batch)
        except Exception:
            pass

        if allow_gc and (aggressive_gc or deleted_git_refs >= 10):
            gc_args = ["git", "gc", "--prune=now", "--quiet"] if aggressive_gc else ["git", "gc", "--auto", "--quiet"]
            gc_timeout = 60 if aggressive_gc else 15
            try:
                subprocess.run(
                    gc_args,
                    cwd=str(config.workspace_root),
                    capture_output=True,
                    text=True,
                    errors="replace",
                    timeout=gc_timeout,
                    check=False,
                )
            except Exception:
                pass

    # Non-Git blob GC & byte-budget cap (250 MB per workspace)
    blobs_dir = config.snapshots_dir / "blobs"
    if blobs_dir.is_dir():
        kept_dir_snaps: list[dict[str, Any]] = []
        seen_blob_sizes: dict[str, int] = {}
        running_bytes = 0
        for s in effective_retained:
            fb = s.get("fileBlobs") if isinstance(s.get("fileBlobs"), dict) else {}
            added_bytes = 0
            new_shas: dict[str, int] = {}
            for sha in fb.values():
                if isinstance(sha, str) and sha and sha not in seen_blob_sizes and sha not in new_shas:
                    bp = blobs_dir / sha[:2] / sha
                    sz = 0
                    if bp.is_file():
                        try:
                            sz = bp.lstat().st_size
                        except OSError:
                            sz = 0
                    new_shas[sha] = sz
                    added_bytes += sz
            if kept_dir_snaps and (running_bytes + added_bytes > MAX_DIR_CACHE_BYTES):
                break
            seen_blob_sizes.update(new_shas)
            running_bytes += added_bytes
            kept_dir_snaps.append(s)

        if len(kept_dir_snaps) < len(effective_retained) and config.git_dir is None:
            deleted_snapshots += len(effective_retained) - len(kept_dir_snaps)
            effective_retained = kept_dir_snaps
            _write_snapshots_json(config.snapshots_path, effective_retained)

        referenced_shas: set[str] = set()
        for s in effective_retained:
            fb = s.get("fileBlobs")
            if isinstance(fb, dict):
                for sha in fb.values():
                    if isinstance(sha, str) and sha:
                        referenced_shas.add(sha)

        for dirpath, _dirnames, filenames in os.walk(blobs_dir, topdown=False, followlinks=False):
            dp = Path(dirpath)
            for fn in filenames:
                if fn not in referenced_shas:
                    fp = dp / fn
                    try:
                        if fp.is_file() and not fp.is_symlink():
                            blob_reclaimed += fp.lstat().st_size
                            fp.unlink(missing_ok=True)
                            deleted_blobs += 1
                    except OSError:
                        continue
            try:
                if dp != blobs_dir and not list(dp.iterdir()):
                    dp.rmdir()
            except OSError:
                pass
        try:
            if not list(blobs_dir.iterdir()):
                blobs_dir.rmdir()
        except OSError:
            pass

    if config.git_dir is None and config.snapshots_dir.is_dir():
        try:
            if not list(config.snapshots_dir.iterdir()):
                config.snapshots_dir.rmdir()
        except OSError:
            pass

    total_reclaimed = legacy_reclaimed + rescued_reclaimed + blob_reclaimed
    return {
        "ok": True,
        "workspace": str(config.workspace_root),
        "retainedCount": len(effective_retained),
        "deletedSnapshots": deleted_snapshots,
        "deletedGitRefs": deleted_git_refs,
        "deletedLegacyDirs": legacy_dirs_deleted,
        "deletedRescuedDirs": rescued_deleted,
        "deletedBlobs": deleted_blobs,
        "reclaimedBytes": total_reclaimed,
        "reclaimedHuman": _format_bytes_human(total_reclaimed),
    }


def load_snapshots(config: BridgeConfig) -> list[dict[str, Any]]:
    """Load ordered list of savepoints (newest first), auto-migrating any legacy .bridge/snapshots* first."""
    if config.legacy_snapshots_path.is_file() or config.legacy_snapshots_dir.is_dir():
        _migrate_legacy_snapshots_if_needed(config, run_prune=True)
    return _read_snapshots_json(config.snapshots_path)


def save_snapshots(
    config: BridgeConfig,
    snapshots: list[dict[str, Any]],
    *,
    allow_gc: bool = True,
) -> Path:
    """Persist savepoints list (retaining up to 25 recent & pruning evicted Git refs/blobs)."""
    trimmed = _filter_retained_snapshots(snapshots, max_keep=_get_max_snapshots(config))
    _write_snapshots_json(config.snapshots_path, trimmed)
    prune_snapshots(config, retained=trimmed, allow_gc=allow_gc)
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

        # Select untracked files within per-file (and for auto_guard, total) size budgets
        saved_untracked: list[str] = []
        total_untracked_bytes = 0
        for rel in untracked_files:
            if deadline is not None and time.monotonic() >= deadline:
                break
            src = root / rel
            try:
                if not src.is_file() or src.is_symlink():
                    continue
                st_size = src.stat().st_size
                if st_size > MAX_BACKUP_FILE_BYTES:
                    continue
                if auto_guard and saved_untracked and (total_untracked_bytes + st_size > MAX_AUTO_UNTRACKED_TOTAL_BYTES):
                    break
                saved_untracked.append(rel)
                total_untracked_bytes += st_size
            except Exception:
                continue

        # Store untracked files content-addressed directly inside .git/objects (zero .bridge/snapshots copies)
        untracked_tree_sha = _write_git_untracked_tree(
            root,
            saved_untracked,
            git_timeout=git_timeout,
        )
        snapshot_commit_sha = _pin_git_snapshot_ref(
            root,
            snap_id=snap_id,
            label=label,
            worktree_tree_sha=worktree_tree_sha,
            head_sha=head_sha,
            index_tree_sha=index_tree_sha,
            untracked_tree_sha=untracked_tree_sha,
            git_timeout=git_timeout,
        )

        record: dict[str, Any] = {
            "id": snap_id,
            "kind": "git",
            "label": label,
            "branch": branch,
            "headSha": head_sha,
            "indexTreeSha": index_tree_sha,
            "treeSha": worktree_tree_sha,
            "untrackedTreeSha": untracked_tree_sha,
            "commitSha": snapshot_commit_sha,
            "ref": f"refs/bridge-snapshots/{snap_id}",
            "untrackedFiles": saved_untracked,
            "autoGuard": bool(auto_guard),
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

        saved_files, file_blobs = _store_dir_blobs(
            root,
            rel_files,
            config.snapshots_dir / "blobs",
            deadline=deadline,
            max_total_bytes=MAX_AUTO_UNTRACKED_TOTAL_BYTES if auto_guard else None,
        )

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
            "fileBlobs": file_blobs,
            "autoGuard": bool(auto_guard),
            "fingerprint": fingerprint,
            "createdAt": now.isoformat(),
            "deduplicated": False,
        }

    existing.insert(0, record)
    save_snapshots(config, existing, allow_gc=not auto_guard)
    return record


def _restore_untracked_from_git_tree(
    root: Path,
    untracked_tree_sha: str,
    allowed_paths: set[str],
) -> list[str]:
    """Extract untracked files from a stored Git tree SHA into root without touching shutil.copystat."""
    restored: list[str] = []
    if not untracked_tree_sha:
        return restored
    try:
        proc = subprocess.run(
            ["git", "archive", "--format=tar", untracked_tree_sha],
            cwd=str(root),
            capture_output=True,
            timeout=15,
            check=False,
        )
        if proc.returncode != 0 or not proc.stdout:
            return restored
        with tarfile.open(fileobj=io.BytesIO(proc.stdout), mode="r:") as tf:
            for member in tf.getmembers():
                if not member.isfile():
                    continue
                rel = member.name[2:] if member.name.startswith("./") else member.name
                rel = rel.lstrip("/")
                if not rel or ".." in Path(rel).parts:
                    continue
                if allowed_paths and rel not in allowed_paths:
                    continue
                extracted = tf.extractfile(member)
                if extracted is None:
                    continue
                dest = root / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(extracted.read())
                restored.append(rel)
    except Exception:
        return restored
    return restored


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

        # Restore untracked files from Git object store (or legacy .bridge/snapshots/<id>/untracked/ fallback)
        restored_untracked: list[str] = []
        ut_tree_sha = str(snap.get("untrackedTreeSha") or "")
        if ut_tree_sha:
            restored_untracked = _restore_untracked_from_git_tree(root, ut_tree_sha, snap_untracked)
        else:
            untracked_backup_dir = config.legacy_snapshots_dir / snap_id / "untracked"
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

    restored_files: list[str] = []
    file_blobs = snap.get("fileBlobs")
    if isinstance(file_blobs, dict) and file_blobs:
        blobs_dir = config.snapshots_dir / "blobs"
        for rel in snap_files:
            sha = file_blobs.get(rel)
            if isinstance(sha, str) and sha:
                blob_path = blobs_dir / sha[:2] / sha
                if blob_path.is_file():
                    dest = root / rel
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    dest.write_bytes(blob_path.read_bytes())
                    restored_files.append(rel)
    else:
        files_backup_dir = config.legacy_snapshots_dir / snap_id / "files"
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


def _discover_scan_workspaces(scan_root: Path) -> list[Path]:
    """Find Git repos, linked .worktrees/*, and .bridge workspaces under scan_root (bounded depth)."""
    resolved_root = scan_root.resolve()
    if not resolved_root.exists():
        return []
    discovered: list[Path] = []
    seen: set[Path] = set()

    def _add(p: Path) -> None:
        try:
            rp = p.resolve()
        except OSError:
            return
        if rp not in seen and rp.is_dir():
            seen.add(rp)
            discovered.append(rp)

    if (resolved_root / ".git").exists() or (resolved_root / ".bridge").is_dir() or (resolved_root / ".bridge.json").is_file():
        _add(resolved_root)

    for dirpath, dirnames, _filenames in os.walk(resolved_root, topdown=True, followlinks=False):
        cur = Path(dirpath)
        try:
            rel_parts = cur.relative_to(resolved_root).parts
        except ValueError:
            dirnames[:] = []
            continue
        depth = len(rel_parts)

        if cur != resolved_root:
            if (cur / ".git").exists():
                _add(cur)
                wt_container = cur / ".worktrees"
                if wt_container.is_dir():
                    for wt in sorted(wt_container.iterdir()):
                        if wt.is_dir() and not wt.is_symlink():
                            _add(wt)
                dirnames[:] = []
                continue
            if (cur / ".bridge").is_dir() or (cur / ".bridge.json").is_file():
                _add(cur)
                dirnames[:] = []
                continue

        if depth >= 3:
            dirnames[:] = []
            continue
        dirnames[:] = sorted(
            d
            for d in dirnames
            if d not in EXCLUDED_BACKUP_DIRS and not (cur / d).is_symlink()
        )

    return discovered


def handle_savepoint(
    args: Any,
    config: BridgeConfig,
    *,
    force_json: bool = False,
    stdout_stream: Any = None,
) -> int:
    """Create, list, or prune local off-branch savepoints."""
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

    if sub in ("prune", "gc", "clean"):
        wipe_all = bool(getattr(args, "all", False))
        raw_keep = getattr(args, "keep", None)
        if not wipe_all and raw_keep is not None and int(raw_keep) < 0:
            emit_output(
                {"ok": False, "error": "INVALID_KEEP_COUNT", "message": "--keep must be >= 0."},
                "Error: --keep must be >= 0.",
                force_json=force_json,
                stdout_stream=stdout_stream,
            )
            return EXIT_ERROR
        effective_keep = 0 if wipe_all else (int(raw_keep) if raw_keep is not None else None)
        scan_arg = str(getattr(args, "scan", None) or "").strip()

        if scan_arg:
            scan_root = Path(scan_arg).expanduser()
            if not scan_root.is_dir():
                emit_output(
                    {"ok": False, "error": "SCAN_DIR_NOT_FOUND", "path": str(scan_root)},
                    f"Error: Scan directory not found: {scan_root}",
                    force_json=force_json,
                    stdout_stream=stdout_stream,
                )
                return EXIT_ERROR
            workspaces = _discover_scan_workspaces(scan_root)
            results: list[dict[str, Any]] = []
            totals = {
                "workspacesPruned": 0,
                "deletedSnapshots": 0,
                "deletedGitRefs": 0,
                "deletedLegacyDirs": 0,
                "deletedRescuedDirs": 0,
                "deletedBlobs": 0,
                "reclaimedBytes": 0,
            }
            for ws in workspaces:
                ws_cfg = resolve_config(cwd=ws, env=config.env)
                st = prune_snapshots(
                    ws_cfg,
                    keep=effective_keep,
                    aggressive_gc=wipe_all,
                    allow_gc=True,
                )
                totals["workspacesPruned"] += 1
                for k in ("deletedSnapshots", "deletedGitRefs", "deletedLegacyDirs", "deletedRescuedDirs", "deletedBlobs", "reclaimedBytes"):
                    totals[k] += int(st.get(k, 0) or 0)
                if (
                    st["deletedSnapshots"]
                    or st["deletedGitRefs"]
                    or st["deletedLegacyDirs"]
                    or st["deletedRescuedDirs"]
                    or st["deletedBlobs"]
                    or st["reclaimedBytes"]
                ):
                    results.append(st)
            totals["reclaimedHuman"] = _format_bytes_human(totals["reclaimedBytes"])
            payload = {"ok": True, "scanRoot": str(scan_root.resolve()), **totals, "details": results}
            emit_output(
                payload,
                [
                    f"Scanned {totals['workspacesPruned']} workspace(s) under {scan_root.resolve()}.",
                    (
                        f"  Reclaimed {totals['reclaimedHuman']} · "
                        f"{totals['deletedLegacyDirs']} legacy dir(s) · "
                        f"{totals['deletedGitRefs']} Git ref(s) · "
                        f"{totals['deletedBlobs']} cache blob(s)"
                    ),
                ],
                force_json=force_json,
                stdout_stream=stdout_stream,
            )
            return EXIT_OK

        stats = prune_snapshots(
            config,
            keep=effective_keep,
            aggressive_gc=wipe_all,
            allow_gc=True,
        )
        emit_output(
            stats,
            [
                f"Pruned savepoints in {config.workspace_root} ({stats['retainedCount']} retained).",
                (
                    f"  Reclaimed {stats['reclaimedHuman']} · "
                    f"{stats['deletedLegacyDirs']} legacy dir(s) · "
                    f"{stats['deletedGitRefs']} Git ref(s) · "
                    f"{stats['deletedBlobs']} cache blob(s)"
                ),
            ],
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
                "message": f"Savepoint '{target_ref}' not found in this workspace.",
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
    "git rev-parse",
    "git ls-files",
    "git ls-tree",
    "git cat-file",
    "git for-each-ref",
    "git worktree list",
    "git blame",
    "git grep",
    "git check-ignore",
    "git config --get",
    "git config --list",
    "git config -l",
    "git shortlog",
    "git describe",
    "git rev-list",
    "gh pr view",
    "gh pr list",
    "gh pr diff",
    "gh pr checks",
    "gh pr status",
    "gh issue view",
    "gh issue list",
    "gh issue status",
    "gh run view",
    "gh run list",
    "gh search",
    "gh repo view",
    "gh release view",
    "gh release list",
    "gh auth status",
    "gh status",
    "brg",
    "bridge",
    "daynote status",
    "dn status",
    "cd",
    "pwd",
    "true",
    "false",
    "which",
    "type",
    "command -v",
    "whoami",
    "id",
    "uname",
    "hostname",
    "date",
    "env",
    "printenv",
    "ls",
    "stat",
    "du",
    "df",
    "file",
    "cat",
    "head",
    "tail",
    "less",
    "wc",
    "echo",
    "printf",
    "rg",
    "grep",
    "egrep",
    "fgrep",
    "jq",
    "lsof",
    "ps",
    "pgrep",
)

READ_ONLY_CHECK_PREFIXES = (
    "pytest",
    "uv run pytest",
    "python -m pytest",
    "python3 -m pytest",
    "npm test",
    "npm run test",
    "npm run lint",
    "npm run typecheck",
    "npm run check",
    "npm run format:check",
    "pnpm test",
    "pnpm run test",
    "pnpm lint",
    "pnpm typecheck",
    "pnpm format:check",
    "yarn test",
    "yarn lint",
    "yarn typecheck",
    "npx vitest",
    "npx jest",
    "npx tsc",
    "npx eslint",
    "npx prettier --check",
    "npx prettier -c",
    "npx biome check",
    "ruff check",
    "uv run ruff check",
    "mypy",
    "uv run mypy",
)

_ENV_ASSIGN_RE = re.compile(r"^(?:[A-Za-z_][A-Za-z0-9_]*=(?:'[^']*'|\"(?:\\.|[^\"\\])*\"|\S*)\s+)+")
_GIT_PATH_OPT_RE = re.compile(r"^git\s+(?:(?:-C|--git-dir|--work-tree)(?:\s+(?:'[^']*'|\"[^\"]*\"|\S+)|=\S+)|--no-pager)\s+")
_QUOTED_LIT_RE = re.compile(r"'[^']*'|\"(?:\\.|[^\"\\])*\"")
_HARMLESS_REDIRECT_RE = re.compile(r"(?:^|\s)(?:2>&1|1>&2|&>/dev/null|[012]?>>/dev/null|[012]?>/dev/null)(?=\s|$)")


def _should_auto_guard_workspace(config: BridgeConfig) -> bool:
    """Only auto-snapshot inside Git repos or explicitly bound Bridge workspaces (never bare ~ or /tmp)."""
    return _is_git_repo(config.workspace_root) or config.is_work_context


def _path_is_within_workspace(raw_path: str, workspace_root: Path) -> bool:
    """Return True if raw_path is relative or resolves inside workspace_root."""
    cleaned = str(raw_path or "").strip()
    if not cleaned:
        return False
    p = Path(cleaned)
    if not p.is_absolute():
        return True
    try:
        ws_resolved = workspace_root.resolve()
        p_resolved = p.resolve()
        return p_resolved == ws_resolved or ws_resolved in p_resolved.parents
    except Exception:
        return False


def _split_shell_segments(cmd: str) -> list[str]:
    """Split a shell command on unquoted &&, ||, ;, and | separators."""
    segments: list[str] = []
    buf: list[str] = []
    in_single = False
    in_double = False
    escape = False
    i = 0
    n = len(cmd)
    while i < n:
        ch = cmd[i]
        if escape:
            buf.append(ch)
            escape = False
            i += 1
            continue
        if ch == "\\" and not in_single:
            buf.append(ch)
            escape = True
            i += 1
            continue
        if ch == "'" and not in_double:
            in_single = not in_single
            buf.append(ch)
            i += 1
            continue
        if ch == '"' and not in_single:
            in_double = not in_double
            buf.append(ch)
            i += 1
            continue
        if not in_single and not in_double:
            if cmd.startswith("&&", i) or cmd.startswith("||", i):
                seg = "".join(buf).strip()
                if seg:
                    segments.append(seg)
                buf.clear()
                i += 2
                continue
            if ch in (";", "|"):
                seg = "".join(buf).strip()
                if seg:
                    segments.append(seg)
                buf.clear()
                i += 1
                continue
        buf.append(ch)
        i += 1
    tail = "".join(buf).strip()
    if tail:
        segments.append(tail)
    return segments


def _matches_cmd_prefix(lower_seg: str, prefix: str) -> bool:
    return lower_seg == prefix or lower_seg.startswith(prefix + " ")


def _is_read_only_bash_command(cmd_str: str) -> bool:
    """Return True when every segment of cmd_str is strictly read-only (with zero file writes/redirections)."""
    stripped = (cmd_str or "").strip()
    if not stripped:
        return True

    # Strip unquoted harmless /dev/null and stderr redirects; reject any remaining unquoted '>' writes
    unquoted = _QUOTED_LIT_RE.sub(" ", stripped)
    unquoted = _HARMLESS_REDIRECT_RE.sub(" ", unquoted)
    if ">" in unquoted:
        return False

    segments = _split_shell_segments(stripped)
    if not segments:
        return True

    for seg in segments:
        s = _HARMLESS_REDIRECT_RE.sub(" ", seg).strip()
        s = _ENV_ASSIGN_RE.sub("", s).strip()
        while True:
            updated = _GIT_PATH_OPT_RE.sub("git ", s).strip()
            if updated == s:
                break
            s = updated
        if not s:
            continue
        lower = s.lower()

        if re.match(r"^(?:tee|sed\s+-i|perl\s+-[a-zA-Z]*i)\b", lower):
            return False

        if _matches_cmd_prefix(lower, "find"):
            if re.search(r"\s(?:-(?:delete|exec|execdir|ok|okdir))\b", lower):
                return False
            continue

        if _matches_cmd_prefix(lower, "git branch"):
            if re.search(r"\s(?:-[dDmMcC]|--(?:delete|move|copy))\b", lower):
                return False
            continue

        if _matches_cmd_prefix(lower, "git tag"):
            if lower == "git tag" or (
                re.search(r"\s(?:-l|--list|--contains|--points-at|--sort|-n)\b", lower)
                and not re.search(r"\s(?:-[dasf]|--delete|--annotate|--sign|--force)\b", lower)
            ):
                continue
            return False

        if _matches_cmd_prefix(lower, "git remote"):
            if lower == "git remote" or re.match(r"^git remote\s+(?:-v|--verbose|show|get-url)(?:\s|$)", lower):
                continue
            return False

        if any(_matches_cmd_prefix(lower, pref) for pref in READ_ONLY_BASH_PREFIXES):
            continue

        if any(_matches_cmd_prefix(lower, pref) for pref in READ_ONLY_CHECK_PREFIXES):
            if re.search(r"\s(?:--fix|--write|-w|-u|--updatesnapshot|--update-snapshots)\b", lower):
                return False
            continue

        return False

    return True


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
        if (
            target_paths
            and _should_auto_guard_workspace(config)
            and any(_path_is_within_workspace(tp, config.workspace_root) for tp in target_paths)
        ):
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
        is_read_only = _is_read_only_bash_command(cmd_str)
        if not is_read_only and cmd_str and _should_auto_guard_workspace(config):
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
