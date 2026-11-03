"""Tests for issue-driven branching (tasks start), P16 fact verification (tasks verify), and agent jobs (list/claim/complete)."""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

from bridge_plugin.cli import run_cli
from bridge_plugin.config import get_branch_state, resolve_config
from bridge_plugin.runtime import EXIT_CONCURRENCY_BLOCK, EXIT_OK


class NonTTYBuffer(io.StringIO):
    def isatty(self) -> bool:
        return False


def _setup_env(tmp_path: Path) -> dict[str, str]:
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
        "BRIDGE_API_KEY": "xrkey_v1.tasks_test_key",
        "BRIDGE_CREDENTIALS_PATH": str(tmp_path / "creds.json"),
    }


def test_tasks_start_binds_branch_state_and_blocks_on_decision(tmp_path: Path) -> None:
    """Verify `bridge tasks start` binds branch -> taskId in .bridge/state.json and exits 2 if BLOCKED_ON_DECISION."""
    env = _setup_env(tmp_path)

    def mock_tasks_transport(_req: Any, _timeout: float) -> tuple[int, bytes]:
        return 200, json.dumps(
            {
                "ok": True,
                "tasks": [
                    {"id": "task_104", "title": "Implement OAuth PKCE", "status": "TODO"},
                    {"id": "task_blocked", "title": "Migrate Billing Schema", "status": "BLOCKED_ON_DECISION"},
                ],
            }
        ).encode("utf-8")

    out_start = NonTTYBuffer()
    code = run_cli(
        ["tasks", "start", "task_104", "--no-checkout"],
        cwd=tmp_path,
        env=env,
        stdout_stream=out_start,
        transport=mock_tasks_transport,
    )
    assert code == EXIT_OK
    start_data = json.loads(out_start.getvalue())
    assert start_data["branch"] == "feat/task_104-implement-oauth-pkce"

    cfg = resolve_config(cwd=tmp_path, env=env)
    bound = get_branch_state(cfg, branch="feat/task_104-implement-oauth-pkce")
    assert bound["taskId"] == "task_104"

    # Attempting to start a task that is BLOCKED_ON_DECISION exits with code 2
    out_blocked = NonTTYBuffer()
    code_blocked = run_cli(
        ["tasks", "start", "task_blocked", "--no-checkout"],
        cwd=tmp_path,
        env=env,
        stdout_stream=out_blocked,
        transport=mock_tasks_transport,
    )
    assert code_blocked == EXIT_CONCURRENCY_BLOCK
    assert json.loads(out_blocked.getvalue())["error"] == "BLOCKED_ON_DECISION"


def test_tasks_verify_bounds_280_chars_and_posts_fact(tmp_path: Path) -> None:
    """Verify `bridge tasks verify` truncates statements to <=280 chars and posts to /api/v1/ingest/facts."""
    env = _setup_env(tmp_path)
    captured: list[dict[str, Any]] = []

    def mock_transport(req: Any, _timeout: float) -> tuple[int, bytes]:
        captured.append(json.loads(req.data.decode("utf-8")))
        return 200, json.dumps({"ok": True, "ingestedCount": 1}).encode("utf-8")

    long_statement = "Verified PKCE flow " * 25  # > 400 chars
    out = NonTTYBuffer()
    code = run_cli(
        ["tasks", "verify", "task_104", long_statement],
        cwd=tmp_path,
        env=env,
        stdout_stream=out,
        transport=mock_transport,
    )
    assert code == EXIT_OK
    assert len(captured) == 1
    fact = captured[0]["facts"][0]
    assert len(fact["statement"]) <= 280
    assert fact["ticketRefs"] == ["task_104"]


def test_jobs_claim_conflict_and_complete(tmp_path: Path) -> None:
    """Verify `bridge jobs claim` returns exit code 2 on 409 conflict and binds jobId on success."""
    env = {**_setup_env(tmp_path), "BRIDGE_BRANCH": "feat/job_88"}

    def conflict_transport(_req: Any, _timeout: float) -> tuple[int, bytes]:
        return 409, json.dumps(
            {
                "ok": False,
                "error": "LEASE_CONFLICT",
                "message": "Job already claimed by another agent",
            }
        ).encode("utf-8")

    out_conflict = NonTTYBuffer()
    code_conflict = run_cli(
        ["jobs", "claim", "job_88", "--globs", "src/auth/*"],
        cwd=tmp_path,
        env=env,
        stdout_stream=out_conflict,
        transport=conflict_transport,
    )
    assert code_conflict == EXIT_CONCURRENCY_BLOCK

    def success_transport(req: Any, _timeout: float) -> tuple[int, bytes]:
        body = json.loads(req.data.decode("utf-8")) if req.data else {}
        return 200, json.dumps(
            {
                "ok": True,
                "action": body.get("action"),
                "job": {"id": "job_88", "taskId": "task_88", "status": "CLAIMED"},
            }
        ).encode("utf-8")

    out_claim = NonTTYBuffer()
    code_claim = run_cli(
        ["jobs", "claim", "job_88", "--globs", "src/auth/*"],
        cwd=tmp_path,
        env=env,
        stdout_stream=out_claim,
        transport=success_transport,
    )
    assert code_claim == EXIT_OK

    # Complete job without passing job_id explicitly (auto-inherited from .bridge/state.json)
    out_complete = NonTTYBuffer()
    code_complete = run_cli(
        ["jobs", "complete", "--pr", "https://github.com/northstar/web-app/pull/12"],
        cwd=tmp_path,
        env=env,
        stdout_stream=out_complete,
        transport=success_transport,
    )
    assert code_complete == EXIT_OK
    assert json.loads(out_complete.getvalue())["action"] == "complete"


def test_tasks_create_jobs_dispatch_and_memory_pin(tmp_path: Path) -> None:
    """Verify `bridge tasks create`, `bridge jobs dispatch`, and `bridge memory pin`."""
    env = _setup_env(tmp_path)
    captured_requests: list[tuple[str, str, dict[str, Any]]] = []

    def mock_transport(req: Any, _timeout: float) -> tuple[int, bytes]:
        body = json.loads(req.data.decode("utf-8")) if req.data else {}
        captured_requests.append((req.get_method(), req.full_url, body))
        if "/api/v1/tasks" in req.full_url and req.get_method() == "POST":
            return 201, json.dumps(
                {
                    "ok": True,
                    "task": {
                        "id": "task_wave_4",
                        "title": body.get("title"),
                        "status": body.get("status", "TODO"),
                    },
                }
            ).encode("utf-8")
        if "/api/v1/fleet/capsules" in req.full_url and req.get_method() == "POST":
            return 201, json.dumps(
                {
                    "ok": True,
                    "capsule": {"id": "cap_wave_4", "title": body.get("title")},
                }
            ).encode("utf-8")
        if "/api/v1/fleet/jobs" in req.full_url and req.get_method() == "POST":
            return 201, json.dumps(
                {
                    "ok": True,
                    "job": {
                        "id": "job_wave_4",
                        "taskId": body.get("taskId"),
                        "capsuleId": body.get("capsuleId"),
                        "status": "QUEUED",
                    },
                }
            ).encode("utf-8")
        if "/api/v1/ingest/topology" in req.full_url and req.get_method() == "POST":
            return 200, json.dumps(
                {
                    "ok": True,
                    "upsertedPinCount": len(body.get("pins", [])),
                    "upsertedChunkCount": len(body.get("chunks", [])),
                    "pins": body.get("pins", []),
                }
            ).encode("utf-8")
        return 200, json.dumps({"ok": True}).encode("utf-8")

    # 1. Create task
    out_create = NonTTYBuffer()
    code_create = run_cli(
        [
            "tasks",
            "create",
            "Wave 4: Zapier, Make.com & n8n Automation Connectors",
            "--description",
            "Audit developer platform docs and ship setup guides",
            "--provider",
            "bridge_cli",
        ],
        cwd=tmp_path,
        env=env,
        stdout_stream=out_create,
        transport=mock_transport,
    )
    assert code_create == EXIT_OK
    created_data = json.loads(out_create.getvalue())
    assert created_data["task"]["id"] == "task_wave_4"

    # 2. Dispatch job
    out_dispatch = NonTTYBuffer()
    code_dispatch = run_cli(
        [
            "jobs",
            "dispatch",
            "Audit Zapier, Make.com, and n8n",
            "--prompt",
            "Verify OAuth2 vs API Key (xrkey_v1) and REST Hook triggers",
            "--task",
            "task_wave_4",
            "--globs",
            "src/lib/workspace/*,src/components/cockpit/*",
        ],
        cwd=tmp_path,
        env=env,
        stdout_stream=out_dispatch,
        transport=mock_transport,
    )
    assert code_dispatch == EXIT_OK
    dispatched_data = json.loads(out_dispatch.getvalue())
    assert dispatched_data["job"]["id"] == "job_wave_4"
    assert dispatched_data["contextCapsuleId"] == "cap_wave_4"

    # 3. Pin guide from file into Company Memory
    guide_file = tmp_path / "zapier-make-n8n-guide.md"
    guide_file.write_text("# Zapier, Make.com & n8n Setup Guide\nStep 1: Generate xrkey_v1 API key.", encoding="utf-8")
    out_pin = NonTTYBuffer()
    code_pin = run_cli(
        [
            "memory",
            "pin",
            "Zapier, Make.com & n8n Setup Guide",
            "--url",
            "https://bridge.xray.tech/integrations#automation",
            "--provider",
            "zapier",
            "--file",
            str(guide_file),
        ],
        cwd=tmp_path,
        env=env,
        stdout_stream=out_pin,
        transport=mock_transport,
    )
    assert code_pin == EXIT_OK
    pin_data = json.loads(out_pin.getvalue())
    assert pin_data["upsertedPinCount"] == 1
    assert pin_data["upsertedChunkCount"] == 1


def test_tasks_capsule_single_and_compile_all(tmp_path: Path) -> None:
    """Verify `bridge tasks capsule <taskId>` and `bridge tasks capsule --compile-all`."""
    env = _setup_env(tmp_path)

    def capsule_transport(req: Any, _timeout: float) -> tuple[int, bytes]:
        body = json.loads(req.data.decode("utf-8")) if req.data else {}
        if body.get("compileAllOpen"):
            return 201, json.dumps(
                {
                    "ok": True,
                    "compiledCount": 2,
                    "capsules": [
                        {"id": "cap_1", "taskId": "t_1", "version": 1},
                        {"id": "cap_2", "taskId": "t_2", "version": 1},
                    ],
                }
            ).encode("utf-8")
        return 201, json.dumps(
            {
                "ok": True,
                "capsule": {
                    "id": "cap_single",
                    "taskId": body.get("taskId"),
                    "version": 1,
                    "compiledMarkdown": f"# Execution Context Capsule: {body.get('taskId')}\n## 4. Command-Line Execution & Write-Back Protocol (`brg`)",
                },
            }
        ).encode("utf-8")

    out_single = NonTTYBuffer()
    code_single = run_cli(
        ["tasks", "capsule", "task_abc"],
        cwd=tmp_path,
        env=env,
        stdout_stream=out_single,
        transport=capsule_transport,
    )
    assert code_single == EXIT_OK
    single_payload = json.loads(out_single.getvalue())
    assert single_payload["capsule"]["taskId"] == "task_abc"
    assert "Command-Line Execution" in single_payload["capsule"]["compiledMarkdown"]

    out_all = NonTTYBuffer()
    code_all = run_cli(
        ["tasks", "capsule", "--compile-all"],
        cwd=tmp_path,
        env=env,
        stdout_stream=out_all,
        transport=capsule_transport,
    )
    assert code_all == EXIT_OK
    all_payload = json.loads(out_all.getvalue())
    assert all_payload["compiledCount"] == 2

    # Verify trailing --org flag on subcommands (`brg context --org org_override` and `brg tasks capsule <id> --org=org_override`)
    captured_orgs: list[str] = []

    def org_transport(req: Any, _timeout: float) -> tuple[int, bytes]:
        if "/api/v1/fleet/context" in req.full_url:
            captured_orgs.append(req.full_url)
            return 200, json.dumps(
                {
                    "ok": True,
                    "organizationId": "org_override",
                    "alignment": {"status": "GREEN", "badge": "🟢 Green", "reasons": []},
                    "tasks": [],
                }
            ).encode("utf-8")
        body = json.loads(req.data.decode("utf-8")) if req.data else {}
        captured_orgs.append(str(body.get("organizationId") or ""))
        return 201, json.dumps(
            {
                "ok": True,
                "capsule": {"id": "cap_org", "taskId": body.get("taskId"), "version": 1},
            }
        ).encode("utf-8")

    out_ctx = NonTTYBuffer()
    code_ctx = run_cli(
        ["context", "--org", "org_override"],
        cwd=tmp_path,
        env=env,
        stdout_stream=out_ctx,
        transport=org_transport,
    )
    assert code_ctx == EXIT_OK
    assert any("org_override" in u for u in captured_orgs)

    out_cap_org = NonTTYBuffer()
    code_cap_org = run_cli(
        ["tasks", "capsule", "task_abc", "--org=org_override_eq"],
        cwd=tmp_path,
        env=env,
        stdout_stream=out_cap_org,
        transport=org_transport,
    )
    assert code_cap_org == EXIT_OK
    assert "org_override_eq" in captured_orgs


