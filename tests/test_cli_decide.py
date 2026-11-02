"""Tests for 1-Click Decisions governance: list, show, propose, score, resolve, and check (exit code 2)."""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

from bridge_plugin.cli import run_cli
from bridge_plugin.config import bind_branch_state, resolve_config
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
        "BRIDGE_API_KEY": "xrkey_v1.decide_test_key",
        "BRIDGE_CREDENTIALS_PATH": str(tmp_path / "creds.json"),
        "BRIDGE_BRANCH": "feat/task_200-cache",
    }


def test_decide_propose_score_resolve_and_show(tmp_path: Path) -> None:
    """Verify proposing a decision with options & weighted criteria, scoring, showing, and resolving."""
    env = _setup_env(tmp_path)
    cfg = resolve_config(cwd=tmp_path, env=env)
    bind_branch_state(cfg, "feat/task_200-cache", task_id="task_200", job_id="job_200")

    requests_log: list[dict[str, Any]] = []

    def mock_transport(req: Any, _timeout: float) -> tuple[int, bytes]:
        body = json.loads(req.data.decode("utf-8")) if req.data else {}
        requests_log.append({"method": req.get_method(), "url": req.full_url, "body": body})
        if req.get_method() == "GET":
            return 200, json.dumps(
                {
                    "ok": True,
                    "decisions": [
                        {
                            "id": "dec_501",
                            "title": "Queue Backend Selection",
                            "contextSummary": "Need durable job scheduling",
                            "status": "OPEN",
                            "urgency": "HIGH",
                            "blockedTaskId": "task_200",
                            "options": [
                                {"id": "opt_1", "label": "Postgres SKIP LOCKED"},
                                {"id": "opt_2", "label": "Redis Streams"},
                            ],
                            "rankings": [
                                {"optionId": "opt_1", "label": "Postgres SKIP LOCKED", "rank": 1, "weightedScore": 8.8}
                            ],
                        }
                    ],
                }
            ).encode("utf-8")
        return 200, json.dumps(
            {
                "ok": True,
                "decisionId": "dec_501",
                "decision": {"id": "dec_501", "title": body.get("title", "Queue Backend Selection")},
            }
        ).encode("utf-8")

    # 1. Propose decision (auto-inherits blockedTaskId=task_200 from branch state)
    out_prop = NonTTYBuffer()
    code_prop = run_cli(
        [
            "decide",
            "propose",
            "--title",
            "Queue Backend Selection",
            "--context",
            "Need durable job scheduling",
            "--urgency",
            "HIGH",
            "--options",
            "Postgres SKIP LOCKED,Redis Streams",
            "--criteria",
            "Durability:9,Ops Simplicity:8",
        ],
        cwd=tmp_path,
        env=env,
        stdout_stream=out_prop,
        transport=mock_transport,
    )
    assert code_prop == EXIT_OK
    prop_body = requests_log[-1]["body"]
    assert prop_body["blockedTaskId"] == "task_200"
    assert len(prop_body["options"]) == 2
    assert prop_body["criteria"] == [
        {"name": "Durability", "weight": 9},
        {"name": "Ops Simplicity", "weight": 8},
    ]

    # 2. Score option 0 on criterion 0
    out_score = NonTTYBuffer()
    code_score = run_cli(
        [
            "decide",
            "score",
            "dec_501",
            "--option",
            "0",
            "--criterion",
            "0",
            "--score",
            "9",
            "--rationale",
            "Zero extra infra",
        ],
        cwd=tmp_path,
        env=env,
        stdout_stream=out_score,
        transport=mock_transport,
    )
    assert code_score == EXIT_OK
    assert "/api/v1/decide/dec_501/score" in requests_log[-1]["url"]
    assert requests_log[-1]["body"]["scores"][0]["score"] == 9

    # 3. Show decision
    out_show = NonTTYBuffer()
    assert (
        run_cli(
            ["decide", "show", "dec_501"],
            cwd=tmp_path,
            env=env,
            stdout_stream=out_show,
            transport=mock_transport,
        )
        == EXIT_OK
    )
    assert json.loads(out_show.getvalue())["decision"]["id"] == "dec_501"

    # 4. Resolve decision
    out_res = NonTTYBuffer()
    assert (
        run_cli(
            ["decide", "resolve", "dec_501", "--option", "opt_1", "--rationale", "Approved by team"],
            cwd=tmp_path,
            env=env,
            stdout_stream=out_res,
            transport=mock_transport,
        )
        == EXIT_OK
    )
    assert requests_log[-1]["body"]["action"] == "ENDORSE"
    assert requests_log[-1]["body"]["chosenOptionId"] == "opt_1"


def test_decide_check_returns_exit_code_2_when_blocked(tmp_path: Path) -> None:
    """Verify `bridge decide check` exits with code 2 when an open decision blocks the active task."""
    env = _setup_env(tmp_path)
    cfg = resolve_config(cwd=tmp_path, env=env)
    bind_branch_state(cfg, "feat/task_200-cache", task_id="task_200")

    def blocked_transport(_req: Any, _timeout: float) -> tuple[int, bytes]:
        return 200, json.dumps(
            {
                "ok": True,
                "decisions": [
                    {
                        "id": "dec_block_1",
                        "title": "Auth Provider Choice",
                        "status": "OPEN",
                        "blockedTaskId": "task_200",
                    }
                ],
            }
        ).encode("utf-8")

    out_blocked = NonTTYBuffer()
    code_blocked = run_cli(
        ["decide", "check"],
        cwd=tmp_path,
        env=env,
        stdout_stream=out_blocked,
        transport=blocked_transport,
    )
    assert code_blocked == EXIT_CONCURRENCY_BLOCK
    parsed = json.loads(out_blocked.getvalue())
    assert parsed["blocked"] is True
    assert parsed["error"] == "BLOCKED_ON_DECISION"

    def clear_transport(_req: Any, _timeout: float) -> tuple[int, bytes]:
        return 200, json.dumps({"ok": True, "decisions": []}).encode("utf-8")

    out_clear = NonTTYBuffer()
    code_clear = run_cli(
        ["decide", "check"],
        cwd=tmp_path,
        env=env,
        stdout_stream=out_clear,
        transport=clear_transport,
    )
    assert code_clear == EXIT_OK
    assert json.loads(out_clear.getvalue())["blocked"] is False
