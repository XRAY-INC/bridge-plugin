"""Tests for seam logging, branch task inheritance, stdin piping, offline spooling (exit 4), and idempotent flush."""

from __future__ import annotations

import io
import json
import urllib.error
from pathlib import Path
from typing import Any

from bridge_plugin.cli import run_cli
from bridge_plugin.config import bind_branch_state, resolve_config
from bridge_plugin.runtime import EXIT_ERROR, EXIT_OFFLINE_SPOOLED, EXIT_OK


class NonTTYBuffer(io.StringIO):
    def isatty(self) -> bool:
        return False


def _setup_bound_workspace(tmp_path: Path) -> dict[str, str]:
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
    return {
        "BRIDGE_API_KEY": "xrkey_v1.northstar_test_token",
        "BRIDGE_CREDENTIALS_PATH": str(tmp_path / "creds.json"),
        "BRIDGE_BRANCH": "feat/task_99-auth",
        "BRIDGE_COMMIT_SHA": "a1b2c3d4e5f6",
    }


def test_seam_inherits_branch_task_and_supports_stdin(tmp_path: Path) -> None:
    """Verify `bridge seam win -` reads from stdin and automatically attaches the branch's bound taskId."""
    env = _setup_bound_workspace(tmp_path)
    cfg = resolve_config(cwd=tmp_path, env=env)
    bind_branch_state(cfg, "feat/task_99-auth", task_id="task_99", job_id="job_77")

    captured_requests: list[dict[str, Any]] = []

    def mock_transport(req: Any, _timeout: float) -> tuple[int, bytes]:
        body = json.loads(req.data.decode("utf-8")) if req.data else {}
        captured_requests.append(
            {
                "method": req.get_method(),
                "url": req.full_url,
                "headers": dict(req.header_items()),
                "body": body,
            }
        )
        return 201, json.dumps(
            {
                "ok": True,
                "deduplicated": False,
                "seam": {"id": "seam_1001", **body},
            }
        ).encode("utf-8")

    out = NonTTYBuffer()
    code = run_cli(
        ["seam", "win", "-"],
        cwd=tmp_path,
        env=env,
        stdin_stream=io.StringIO("Shipped OAuth 2.1 PKCE token exchange\n"),
        stdout_stream=out,
        transport=mock_transport,
    )
    assert code == EXIT_OK
    res_json = json.loads(out.getvalue())
    assert res_json["ok"] is True
    assert res_json["taskId"] == "task_99"
    assert res_json["jobId"] == "job_77"
    assert len(captured_requests) == 1
    sent_body = captured_requests[0]["body"]
    assert sent_body["tag"] == "WIN"
    assert sent_body["taskId"] == "task_99"
    assert sent_body["summary"] == "Shipped OAuth 2.1 PKCE token exchange"

    # Edit and retract 'last' seam
    out_edit = NonTTYBuffer()
    code_edit = run_cli(
        ["seam", "edit", "last", "Shipped OAuth 2.1 PKCE + refresh rotation"],
        cwd=tmp_path,
        env=env,
        stdout_stream=out_edit,
        transport=mock_transport,
    )
    assert code_edit == EXIT_OK
    assert captured_requests[-1]["method"] == "PATCH"
    assert captured_requests[-1]["body"]["id"] == "seam_1001"

    out_retract = NonTTYBuffer()
    code_retract = run_cli(
        ["seam", "retract", "last"],
        cwd=tmp_path,
        env=env,
        stdout_stream=out_retract,
        transport=mock_transport,
    )
    assert code_retract == EXIT_OK
    assert captured_requests[-1]["method"] == "DELETE"
    assert captured_requests[-1]["body"]["id"] == "seam_1001"


def test_offline_spooling_deduplication_and_idempotent_flush(tmp_path: Path) -> None:
    """Verify network failure spools to .bridge/spool.jsonl with exit code 4 and flushes idempotently."""
    env = _setup_bound_workspace(tmp_path)

    def failing_transport(_req: Any, _timeout: float) -> tuple[int, bytes]:
        raise urllib.error.URLError("Connection refused")

    out1 = NonTTYBuffer()
    code1 = run_cli(
        ["seam", "decision", "Selected Postgres LISTEN/NOTIFY for cache invalidation"],
        cwd=tmp_path,
        env=env,
        stdout_stream=out1,
        transport=failing_transport,
    )
    assert code1 == EXIT_OFFLINE_SPOOLED
    data1 = json.loads(out1.getvalue())
    assert data1["spooled"] is True
    assert data1["deduplicated"] is False

    # Submitting the exact same seam while still offline deduplicates in .bridge/spool.jsonl
    out2 = NonTTYBuffer()
    code2 = run_cli(
        ["seam", "decision", "Selected Postgres LISTEN/NOTIFY for cache invalidation"],
        cwd=tmp_path,
        env=env,
        stdout_stream=out2,
        transport=failing_transport,
    )
    assert code2 == EXIT_OFFLINE_SPOOLED
    data2 = json.loads(out2.getvalue())
    assert data2["spooled"] is True
    assert data2["deduplicated"] is True

    spool_file = tmp_path / ".bridge" / "spool.jsonl"
    assert spool_file.is_file()
    lines = [l for l in spool_file.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(lines) == 1

    # Flush spool when connection is restored
    flushed_Payloads: list[dict[str, Any]] = []

    def online_transport(req: Any, _timeout: float) -> tuple[int, bytes]:
        flushed_Payloads.append(json.loads(req.data.decode("utf-8")))
        return 201, json.dumps({"ok": True, "deduplicated": False}).encode("utf-8")

    out_flush = NonTTYBuffer()
    code_flush = run_cli(
        ["seam", "flush"],
        cwd=tmp_path,
        env=env,
        stdout_stream=out_flush,
        transport=online_transport,
    )
    assert code_flush == EXIT_OK
    flush_data = json.loads(out_flush.getvalue())
    assert flush_data["flushedCount"] == 1
    assert flush_data["remainingCount"] == 0
    assert not spool_file.exists()
    assert len(flushed_Payloads) == 1


def test_scratchpad_and_handoff_commands(tmp_path: Path) -> None:
    """Verify scratchpad read/write and handoff commands."""
    env = _setup_bound_workspace(tmp_path)
    calls: list[str] = []

    def mock_transport(req: Any, _timeout: float) -> tuple[int, bytes]:
        calls.append(f"{req.get_method()} {req.full_url}")
        if req.get_method() == "GET":
            return 200, json.dumps(
                {
                    "ok": True,
                    "notes": [
                        {
                            "topic": "northstar/web-app (main)",
                            "repository": "northstar/web-app",
                            "gitBranch": "main",
                            "agentId": "claude-code",
                            "bodyMarkdown": "Ready for staging deploy",
                        }
                    ],
                }
            ).encode("utf-8")
        return 200, json.dumps({"ok": True, "seam": {"id": "seam_h1"}}).encode("utf-8")

    out_write = NonTTYBuffer()
    assert (
        run_cli(
            ["scratchpad", "write", "Arch Notes", "Use HKDF per-tenant envelopes"],
            cwd=tmp_path,
            env=env,
            stdout_stream=out_write,
            transport=mock_transport,
        )
        == EXIT_OK
    )

    out_read = NonTTYBuffer()
    assert (
        run_cli(
            ["scratchpad", "read"],
            cwd=tmp_path,
            env=env,
            stdout_stream=out_read,
            transport=mock_transport,
        )
        == EXIT_OK
    )
    assert len(json.loads(out_read.getvalue())["notes"]) == 1

    out_handoff = NonTTYBuffer()
    assert (
        run_cli(
            ["handoff", "Completed Phase 1 and Phase 2"],
            cwd=tmp_path,
            env=env,
            stdout_stream=out_handoff,
            transport=mock_transport,
        )
        == EXIT_OK
    )
    assert any("/api/v1/fleet/seam" in c for c in calls)
    assert any("/api/v1/fleet/scratchpad" in c for c in calls)


def test_seam_historical_and_backdated_timestamps(tmp_path: Path) -> None:
    """Verify --at / --date / --created-at and --historical on seam log, edit, and list."""
    env = _setup_bound_workspace(tmp_path)
    captured_requests: list[dict[str, Any]] = []

    def mock_transport(req: Any, _timeout: float) -> tuple[int, bytes]:
        body = json.loads(req.data.decode("utf-8")) if req.data else {}
        captured_requests.append(
            {
                "method": req.get_method(),
                "url": req.full_url,
                "body": body,
            }
        )
        if req.get_method() == "GET":
            return 200, json.dumps(
                {
                    "ok": True,
                    "seams": [
                        {
                            "id": "seam_hist_101",
                            "tag": "WIN",
                            "historical": True,
                            "repository": "northstar/web-app",
                            "gitBranch": "main",
                            "summary": "Shipped v1 Stripe webhook retry queue",
                        }
                    ],
                }
            ).encode("utf-8")
        return 201, json.dumps(
            {
                "ok": True,
                "deduplicated": False,
                "seam": {"id": "seam_hist_101", **body},
            }
        ).encode("utf-8")

    out_log = NonTTYBuffer()
    code_log = run_cli(
        [
            "seam",
            "win",
            "Shipped v1 Stripe webhook retry queue",
            "--at",
            "2026-10-15",
            "--historical",
        ],
        cwd=tmp_path,
        env=env,
        stdout_stream=out_log,
        transport=mock_transport,
    )
    assert code_log == EXIT_OK
    res_log = json.loads(out_log.getvalue())
    assert res_log["historical"] is True
    assert res_log["createdAt"] == "2026-10-15T12:00:00Z"
    sent_log = captured_requests[-1]["body"]
    assert sent_log["createdAt"] == "2026-10-15T12:00:00Z"
    assert sent_log["loggedAt"] == "2026-10-15T12:00:00Z"
    assert sent_log["historical"] is True
    assert sent_log["metadata"]["historical"] is True
    assert sent_log["metadata"]["createdAt"] == "2026-10-15T12:00:00Z"

    # ISO-8601 with timezone offset via --date alias
    out_iso = NonTTYBuffer()
    assert (
        run_cli(
            [
                "seam",
                "decision",
                "Selected Postgres advisory locks",
                "--date",
                "2026-10-20T09:30:00-04:00",
            ],
            cwd=tmp_path,
            env=env,
            stdout_stream=out_iso,
            transport=mock_transport,
        )
        == EXIT_OK
    )
    assert captured_requests[-1]["body"]["createdAt"] == "2026-10-20T13:30:00Z"
    assert "historical" not in captured_requests[-1]["body"]

    # Invalid calendar date and future date fail locally with EXIT_ERROR without network call
    prev_count = len(captured_requests)
    out_bad = NonTTYBuffer()
    assert (
        run_cli(
            ["seam", "win", "Bad date", "--at", "2026-02-31"],
            cwd=tmp_path,
            env=env,
            stdout_stream=out_bad,
            transport=mock_transport,
        )
        == EXIT_ERROR
    )
    assert json.loads(out_bad.getvalue())["error"] == "INVALID_SEAM_TIMESTAMP"
    assert len(captured_requests) == prev_count

    out_future = NonTTYBuffer()
    assert (
        run_cli(
            ["seam", "win", "Future date", "--at", "2099-01-01"],
            cwd=tmp_path,
            env=env,
            stdout_stream=out_future,
            transport=mock_transport,
        )
        == EXIT_ERROR
    )
    assert json.loads(out_future.getvalue())["error"] == "FUTURE_SEAM_TIMESTAMP"
    assert len(captured_requests) == prev_count

    # Edit seam timestamp + historical flag without needing to repeat summary
    out_edit = NonTTYBuffer()
    assert (
        run_cli(
            ["seam", "edit", "last", "--at", "2026-10-14", "--historical"],
            cwd=tmp_path,
            env=env,
            stdout_stream=out_edit,
            transport=mock_transport,
        )
        == EXIT_OK
    )
    assert captured_requests[-1]["method"] == "PATCH"
    assert captured_requests[-1]["body"]["createdAt"] == "2026-10-14T12:00:00Z"
    assert captured_requests[-1]["body"]["historical"] is True

    # List filtered by --historical true
    out_list = NonTTYBuffer()
    assert (
        run_cli(
            ["seam", "list", "--historical", "true"],
            cwd=tmp_path,
            env=env,
            stdout_stream=out_list,
            transport=mock_transport,
        )
        == EXIT_OK
    )
    assert "historical=true" in captured_requests[-1]["url"]


def test_seam_project_move_link_migration_and_projects_sync(tmp_path: Path) -> None:
    """Verify `brg link` migrates root `.bridge.json` into `.bridge/bridge.json`, syncs `/api/v1/projects`,
    and `brg seam edit --project --bind-repo` + `brg projects set` work end-to-end.
    """
    import subprocess

    subprocess.run(["git", "init", "-b", "main"], cwd=str(tmp_path), capture_output=True, check=True)
    (tmp_path / ".gitignore").write_text(".bridge/\n", encoding="utf-8")

    # Seed an untracked legacy root `.bridge.json` with custom emailDomain
    (tmp_path / ".bridge.json").write_text(
        json.dumps(
            {
                "plugin": "bridge",
                "organizationId": "org_acme",
                "apiUrl": "https://bridge.xray.tech",
                "repository": "airtable-sync",
                "emailDomain": "acme.com",
            }
        ),
        encoding="utf-8",
    )
    env = {
        "BRIDGE_API_KEY": "xrkey_v1.acme_token",
        "BRIDGE_CREDENTIALS_PATH": str(tmp_path / "creds.json"),
    }
    captured: list[dict[str, Any]] = []

    def mock_transport(req: Any, _timeout: float) -> tuple[int, bytes]:
        body = json.loads(req.data.decode("utf-8")) if req.data else {}
        captured.append({"method": req.get_method(), "url": req.full_url, "body": body})
        if "/api/v1/projects" in req.full_url:
            if req.get_method() == "GET":
                return 200, json.dumps(
                    {
                        "ok": True,
                        "projects": [
                            {
                                "id": "proj_acme_maint",
                                "name": "Acme Southeast - Maintenance",
                                "repositoryFullName": "airtable-sync",
                                "clientName": "Acme Southeast",
                                "seamCount": 3,
                                "taskCount": 1,
                            }
                        ],
                    }
                ).encode("utf-8")
            return 200, json.dumps(
                {
                    "ok": True,
                    "project": {
                        "id": "proj_acme_maint",
                        "name": body.get("name"),
                        "repositoryFullName": body.get("repositoryFullName"),
                        "clientName": body.get("clientName"),
                    },
                }
            ).encode("utf-8")
        return 200, json.dumps({"ok": True, "seam": {"id": "seam_milestone_1", **body}}).encode("utf-8")

    # 1. `brg link` moves config into `.bridge/bridge.json`, upgrades `.gitignore`, removes untracked `.bridge.json`, and syncs `/api/v1/projects`
    out_link = NonTTYBuffer()
    assert (
        run_cli(
            [
                "link",
                "--repo",
                "airtable-sync",
                "--project",
                "Acme Southeast - Maintenance",
                "--client",
                "Acme Southeast",
                "--no-setup",
            ],
            cwd=tmp_path,
            env=env,
            stdout_stream=out_link,
            transport=mock_transport,
        )
        == EXIT_OK
    )
    link_json = json.loads(out_link.getvalue())
    assert link_json["projectSynced"] is True
    assert link_json["projectId"] == "proj_acme_maint"
    assert (tmp_path / ".bridge" / "bridge.json").is_file()
    assert not (tmp_path / ".bridge.json").exists()

    saved_cfg = json.loads((tmp_path / ".bridge" / "bridge.json").read_text(encoding="utf-8"))
    assert saved_cfg["emailDomain"] == "acme.com"
    assert saved_cfg["projectName"] == "Acme Southeast - Maintenance"
    assert saved_cfg["clientName"] == "Acme Southeast"

    gitignore_text = (tmp_path / ".gitignore").read_text(encoding="utf-8")
    assert ".bridge/*" in gitignore_text
    assert "!.bridge/bridge.json" in gitignore_text

    assert captured[0]["method"] == "POST"
    assert "/api/v1/projects" in captured[0]["url"]
    assert captured[0]["body"]["repositoryFullName"] == "airtable-sync"
    assert captured[0]["body"]["clientName"] == "Acme Southeast"

    # 2. `brg seam edit <id> --project "..." --bind-repo` moves an existing seam without requiring a new summary
    out_edit = NonTTYBuffer()
    assert (
        run_cli(
            [
                "seam",
                "edit",
                "seam_milestone_1",
                "--project",
                "Acme Southeast - Maintenance",
                "--bind-repo",
            ],
            cwd=tmp_path,
            env=env,
            stdout_stream=out_edit,
            transport=mock_transport,
        )
        == EXIT_OK
    )
    assert captured[-1]["method"] == "PATCH"
    assert captured[-1]["body"]["project"] == "Acme Southeast - Maintenance"
    assert captured[-1]["body"]["bindRepositoryToProject"] is True
    assert "summary" not in captured[-1]["body"]

    # 3. `brg projects list` and `brg projects set` work cleanly
    out_proj_list = NonTTYBuffer()
    assert (
        run_cli(["projects", "list"], cwd=tmp_path, env=env, stdout_stream=out_proj_list, transport=mock_transport)
        == EXIT_OK
    )
    assert json.loads(out_proj_list.getvalue())["projects"][0]["repositoryFullName"] == "airtable-sync"


