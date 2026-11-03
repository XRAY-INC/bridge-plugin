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


def test_decide_propose_recipients_and_advisory_mode_check(tmp_path: Path) -> None:
    """Verify `--to kate,doc --mode advisory` distributes 1-Click Magic Links and does not block `brg decide check`."""
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
                            "id": "dec_adv_1",
                            "title": "Staging Promo Cutover Window",
                            "status": "OPEN",
                            "deliveryMode": "ADVISORY",
                            "blockedTaskId": "task_200",
                        }
                    ],
                }
            ).encode("utf-8")
        return 200, json.dumps(
            {
                "ok": True,
                "decisionId": "dec_adv_1",
                "mode": "ADVISORY",
                "decision": {"id": "dec_adv_1", "title": body.get("title"), "deliveryMode": "ADVISORY"},
                "distribution": {
                    "mode": "ADVISORY",
                    "recipients": [
                        {"email": "kate@xray.tech", "name": "Kate", "voteUrl": "https://bridge.xray.tech/decide/guest?token=t1"},
                        {"email": "doc@xray.tech", "name": "Doc", "voteUrl": "https://bridge.xray.tech/decide/guest?token=t2"},
                    ],
                },
                "guestLinks": [
                    {"email": "kate@xray.tech", "voteUrl": "https://bridge.xray.tech/decide/guest?token=t1"},
                    {"email": "doc@xray.tech", "voteUrl": "https://bridge.xray.tech/decide/guest?token=t2"},
                ],
            }
        ).encode("utf-8")

    out_prop = NonTTYBuffer()
    code_prop = run_cli(
        [
            "decide",
            "propose",
            "--title",
            "Staging Promo Cutover Window",
            "--context",
            "Proceeding with Option A unless Kate or Doc vetoes via 1-Click Magic Link",
            "--to",
            "kate,doc",
            "--mode",
            "advisory",
            "--options",
            "Promote immediately,Hold until morning",
            "--criteria",
            "Velocity:9,Safety:8",
            "--scores",
            "0:0:9:Immediate feedback loop,0:1:8:Pre-promo snapshot verified",
            "--flow",
            "n1:Promo Gate:trigger|n2:Production Cutover:impact",
        ],
        cwd=tmp_path,
        env=env,
        stdout_stream=out_prop,
        transport=mock_transport,
    )
    assert code_prop == EXIT_OK
    prop_body = requests_log[-1]["body"]
    assert prop_body["mode"] == "ADVISORY"
    assert prop_body["to"] == ["kate", "doc"]
    assert prop_body["recipients"] == ["kate", "doc"]
    assert len(prop_body["scores"]) == 2
    assert prop_body["scores"][0] == {
        "optionIndex": 0,
        "criterionIndex": 0,
        "score": 9,
        "rationale": "Immediate feedback loop",
    }
    assert len(prop_body["flows"]) == 1

    # Verify `decide check` exits 0 (not 2) when only ADVISORY decisions are open
    out_chk = NonTTYBuffer()
    code_chk = run_cli(
        ["decide", "check"],
        cwd=tmp_path,
        env=env,
        stdout_stream=out_chk,
        transport=mock_transport,
    )
    assert code_chk == EXIT_OK
    chk_parsed = json.loads(out_chk.getvalue())
    assert chk_parsed["blocked"] is False
    assert chk_parsed["blockingDecisions"] == []
    assert len(chk_parsed["advisoryDecisions"]) == 1
    assert chk_parsed["advisoryDecisions"][0]["id"] == "dec_adv_1"


def test_dual_homed_client_and_agency_routing(tmp_path: Path) -> None:
    """Verify Dual-Homed Client + Agency ('XRAY on Client Work') routing:
    - Multi-workspace keyring in credentials.json preserves both Client and Agency keys
    - Default `brg seam win` logs to Client org AND mirrors rollup to Agency org
    - `--internal` routes friction and internal Decision Cards strictly to Agency org
    - `brg context` hydrates both Client context and `agencyContext`
    """
    from bridge_plugin.config import save_user_credentials

    cred_path = tmp_path / "creds.json"
    env = {"BRIDGE_CREDENTIALS_PATH": str(cred_path)}

    # 1. Save both XRAY Agency key and Client Workspace key into keyring
    save_user_credentials(
        api_key="xrkey_v1.xray_agency_key",
        organization_id="org_xray_tech",
        user_email="aris@xray.tech",
        env=env,
    )
    save_user_credentials(
        api_key="xrkey_v1.client_acme_key",
        organization_id="org_client_acme",
        agency_organization_id="org_xray_tech",
        user_email="aris@xray.tech",
        env=env,
    )

    # 2. Bind client repo with `brg init --org org_client_acme --agency-org org_xray_tech`
    out_init = NonTTYBuffer()
    assert (
        run_cli(
            ["init", "--org", "org_client_acme", "--agency-org", "org_xray_tech", "--repo", "acme/platform"],
            cwd=tmp_path,
            env=env,
            stdout_stream=out_init,
        )
        == EXIT_OK
    )
    init_data = json.loads(out_init.getvalue())
    assert init_data["organizationId"] == "org_client_acme"
    assert init_data["agencyOrganizationId"] == "org_xray_tech"

    requests_log: list[dict[str, Any]] = []

    def dual_transport(req: Any, _timeout: float) -> tuple[int, bytes]:
        body = json.loads(req.data.decode("utf-8")) if req.data else {}
        headers = {k.lower(): v for k, v in req.header_items()}
        org_header = headers.get("x-organization-id")
        auth_header = headers.get("authorization")
        requests_log.append(
            {
                "method": req.get_method(),
                "url": req.full_url,
                "org": org_header,
                "auth": auth_header,
                "body": body,
            }
        )
        if req.get_method() == "GET":
            return 200, json.dumps(
                {
                    "ok": True,
                    "organizationId": org_header,
                    "teamRoster": [
                        {"handle": "cto", "name": "Acme CTO", "email": "cto@acme.com"}
                        if org_header == "org_client_acme"
                        else {"handle": "kate", "name": "Kate", "email": "kate@xray.tech"}
                    ],
                    "tasks": [],
                    "decisions": [],
                }
            ).encode("utf-8")
        return 200, json.dumps({"ok": True, "seam": {"id": f"s_{len(requests_log)}"}}).encode("utf-8")

    # 3. Default `brg seam win` logs to Client (org_client_acme) AND mirrors to Agency (org_xray_tech)
    out_win = NonTTYBuffer()
    assert (
        run_cli(
            ["seam", "win", "Shipped SSO login flow for Acme"],
            cwd=tmp_path,
            env=env,
            stdout_stream=out_win,
            transport=dual_transport,
        )
        == EXIT_OK
    )
    win_data = json.loads(out_win.getvalue())
    assert win_data["mirroredToAgencyOrg"] == "org_xray_tech"
    assert requests_log[-2]["org"] == "org_client_acme"
    assert "xrkey_v1.client_acme_key" in requests_log[-2]["auth"]
    assert requests_log[-1]["org"] == "org_xray_tech"
    assert "xrkey_v1.xray_agency_key" in requests_log[-1]["auth"]
    assert "[Client: org_client_acme]" in requests_log[-1]["body"]["summary"]

    # 4. Internal blocker (`brg seam friction --internal`) routes ONLY to org_xray_tech (never Client!)
    before_count = len(requests_log)
    out_fric = NonTTYBuffer()
    assert (
        run_cli(
            ["seam", "friction", "--internal", "Client requested out-of-scope NetSuite sync; holding for SOW check"],
            cwd=tmp_path,
            env=env,
            stdout_stream=out_fric,
            transport=dual_transport,
        )
        == EXIT_OK
    )
    assert len(requests_log) == before_count + 1
    assert requests_log[-1]["org"] == "org_xray_tech"
    assert "xrkey_v1.xray_agency_key" in requests_log[-1]["auth"]

    # 5. Internal Decision Card (`brg decide propose --internal --to kate,doc`) routes ONLY to org_xray_tech
    out_dec_int = NonTTYBuffer()
    assert (
        run_cli(
            [
                "decide",
                "propose",
                "--internal",
                "--title",
                "SOW Scope Check: NetSuite Sync",
                "--context",
                "Client asked for custom NetSuite sync during sprint",
                "--to",
                "kate,doc",
                "--mode",
                "advisory",
            ],
            cwd=tmp_path,
            env=env,
            stdout_stream=out_dec_int,
            transport=dual_transport,
        )
        == EXIT_OK
    )
    assert requests_log[-1]["org"] == "org_xray_tech"

    # 6. Client Decision Card (`brg decide propose --to cto@acme.com`) routes to org_client_acme
    out_dec_client = NonTTYBuffer()
    assert (
        run_cli(
            [
                "decide",
                "propose",
                "--title",
                "Acme SAML Attribute Mapping",
                "--context",
                "Confirm Okta group attribute claim name",
                "--to",
                "cto@acme.com",
                "--mode",
                "advisory",
            ],
            cwd=tmp_path,
            env=env,
            stdout_stream=out_dec_client,
            transport=dual_transport,
        )
        == EXIT_OK
    )
    assert requests_log[-1]["org"] == "org_client_acme"

    # 7. `brg context` hydrates BOTH Client context and `agencyContext`
    out_ctx = NonTTYBuffer()
    assert (
        run_cli(
            ["context", "--json"],
            cwd=tmp_path,
            env=env,
            stdout_stream=out_ctx,
            transport=dual_transport,
        )
        == EXIT_OK
    )
    ctx_data = json.loads(out_ctx.getvalue())
    assert ctx_data["organizationId"] == "org_client_acme"
    assert ctx_data["teamRoster"][0]["email"] == "cto@acme.com"
    assert ctx_data["agencyContext"]["organizationId"] == "org_xray_tech"
    assert ctx_data["agencyContext"]["teamRoster"][0]["email"] == "kate@xray.tech"


