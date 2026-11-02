"""Tests for stdio JSON-RPC 2.0 MCP server: 3 Resources, 3 Prompts, and 6 Consolidated Tools (<900 schema tokens)."""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

from bridge_plugin.client import BridgeClient
from bridge_plugin.config import bind_branch_state, resolve_config
from bridge_plugin.mcp_server import (
    MCP_PROMPTS,
    MCP_RESOURCES,
    MCP_TOOLS,
    handle_jsonrpc_message,
    run_stdio_server,
)


def _make_mcp_fixture(tmp_path: Path) -> tuple[Any, BridgeClient]:
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
    env = {
        "BRIDGE_API_KEY": "xrkey_v1.mcp_test_token",
        "BRIDGE_CREDENTIALS_PATH": str(tmp_path / "creds.json"),
        "BRIDGE_BRANCH": "feat/task_300-mcp",
    }
    cfg = resolve_config(cwd=tmp_path, env=env)
    bind_branch_state(cfg, "feat/task_300-mcp", task_id="task_300", job_id="job_300")

    def mock_transport(req: Any, _timeout: float) -> tuple[int, bytes]:
        url = req.full_url
        if "/api/v1/decide" in url and req.get_method() == "GET":
            return 200, json.dumps(
                {
                    "ok": True,
                    "decisions": [{"id": "dec_1", "title": "Storage Engine", "status": "OPEN"}],
                }
            ).encode("utf-8")
        if "/api/v1/tasks" in url:
            return 200, json.dumps(
                {
                    "ok": True,
                    "tasks": [{"id": "task_300", "title": "Build MCP Server", "status": "IN_PROGRESS"}],
                }
            ).encode("utf-8")
        if "/api/v1/fleet/jobs" in url:
            return 200, json.dumps(
                {
                    "ok": True,
                    "jobs": [{"id": "job_300", "taskId": "task_300", "status": "CLAIMED"}],
                }
            ).encode("utf-8")
        if "/api/v1/fleet/scratchpad" in url:
            return 200, json.dumps(
                {
                    "ok": True,
                    "notes": [{"topic": "MCP", "bodyMarkdown": "Resources + Prompts + 6 Tools"}],
                }
            ).encode("utf-8")
        return 200, json.dumps({"ok": True, "seam": {"id": "seam_mcp_1"}}).encode("utf-8")

    client = BridgeClient(cfg, agent_id="claude-code", transport=mock_transport, env=env)
    return cfg, client


def test_mcp_schema_token_budget_under_900_tokens() -> None:
    """Verify the 6 consolidated tools, 3 resources, and 3 prompts stay strictly under 900 estimated tokens."""
    assert len(MCP_RESOURCES) == 3
    assert len(MCP_PROMPTS) == 3
    assert len(MCP_TOOLS) == 6
    tool_names = [t["name"] for t in MCP_TOOLS]
    assert tool_names == [
        "bridge_context",
        "bridge_seam",
        "bridge_decide",
        "bridge_scope",
        "bridge_job",
        "bridge_fact",
    ]
    serialized = json.dumps(MCP_TOOLS, separators=(",", ":"))
    estimated_tokens = len(serialized) // 4
    assert estimated_tokens < 900, f"Tool schema token count {estimated_tokens} exceeds 900-token budget"


def test_mcp_resources_prompts_and_six_tools(tmp_path: Path) -> None:
    """Verify JSON-RPC 2.0 initialize, resources/read, prompts/get, and tools/call for all 6 tools."""
    cfg, client = _make_mcp_fixture(tmp_path)

    # 1. initialize & notification
    init_res = handle_jsonrpc_message(
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        config=cfg,
        client=client,
    )
    assert init_res is not None
    assert init_res["result"]["serverInfo"]["name"] == "bridge-mcp"
    assert (
        handle_jsonrpc_message(
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            config=cfg,
            client=client,
        )
        is None
    )

    # 2. resources/list & resources/read for all 3 URIs
    res_list = handle_jsonrpc_message(
        {"jsonrpc": "2.0", "id": 2, "method": "resources/list"},
        config=cfg,
        client=client,
    )
    assert len(res_list["result"]["resources"]) == 3

    for uri in ("bridge://context", "bridge://decisions/open", "bridge://tasks/active"):
        read_res = handle_jsonrpc_message(
            {"jsonrpc": "2.0", "id": 3, "method": "resources/read", "params": {"uri": uri}},
            config=cfg,
            client=client,
        )
        contents = read_res["result"]["contents"]
        assert contents[0]["uri"] == uri
        parsed_content = json.loads(contents[0]["text"])
        assert parsed_content["ok"] is True

    # 3. prompts/list & prompts/get for all 3 prompts
    prompts_list = handle_jsonrpc_message(
        {"jsonrpc": "2.0", "id": 4, "method": "prompts/list"},
        config=cfg,
        client=client,
    )
    assert len(prompts_list["result"]["prompts"]) == 3

    for prompt_name in ("bridge-session-brief", "bridge-escalate-decision", "bridge-turn-handoff"):
        p_res = handle_jsonrpc_message(
            {
                "jsonrpc": "2.0",
                "id": 5,
                "method": "prompts/get",
                "params": {"name": prompt_name, "arguments": {"title": "DB", "tradeoff": "SQL vs KV"}},
            },
            config=cfg,
            client=client,
        )
        assert len(p_res["result"]["messages"]) == 1

    # 4. tools/call across all 6 consolidated tools
    tool_calls = [
        ("bridge_context", {}),
        ("bridge_seam", {"action": "log", "tag": "win", "summary": "MCP seam logged"}),
        ("bridge_decide", {"action": "propose", "title": "API Auth", "context": "Bearer vs mTLS"}),
        ("bridge_scope", {"action": "claim", "globs": ["src/mcp/*"], "ttl": "15m"}),
        ("bridge_job", {"action": "list"}),
        ("bridge_fact", {"statement": "MCP stdio server verified with 6 tools", "task_id": "task_300"}),
    ]
    for idx, (tool_name, tool_args) in enumerate(tool_calls, start=10):
        call_res = handle_jsonrpc_message(
            {
                "jsonrpc": "2.0",
                "id": idx,
                "method": "tools/call",
                "params": {"name": tool_name, "arguments": tool_args},
            },
            config=cfg,
            client=client,
        )
        assert call_res is not None
        assert call_res["result"]["isError"] is False
        payload = json.loads(call_res["result"]["content"][0]["text"])
        assert payload["ok"] is True


def test_stdio_framing_loop(tmp_path: Path) -> None:
    """Verify newline-delimited JSON-RPC stdio stream processing."""
    cfg, client = _make_mcp_fixture(tmp_path)
    in_buf = io.StringIO(
        json.dumps({"jsonrpc": "2.0", "id": 101, "method": "ping"})
        + "\n"
        + json.dumps({"jsonrpc": "2.0", "id": 102, "method": "tools/list"})
        + "\n"
    )
    out_buf = io.StringIO()
    run_stdio_server(stdin_stream=in_buf, stdout_stream=out_buf, config=cfg, client=client)
    lines = [json.loads(line) for line in out_buf.getvalue().splitlines() if line.strip()]
    assert len(lines) == 2
    assert lines[0]["id"] == 101
    assert len(lines[1]["result"]["tools"]) == 6
