"""Tests for stdio JSON-RPC 2.0 MCP server, bridge context CLI, bridge_context_pull, bridge_brief_latest, and universal skills."""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

from bridge_plugin.cli import run_cli
from bridge_plugin.client import BridgeClient
from bridge_plugin.config import bind_branch_state, resolve_config
from bridge_plugin.mcp_server import (
    MCP_PROMPTS,
    MCP_RESOURCES,
    MCP_TOOLS,
    handle_jsonrpc_message,
    run_stdio_server,
)
from bridge_plugin.runtime import EXIT_OK


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
        if "/api/v1/fleet/context" in url and req.get_method() == "GET":
            return 200, json.dumps(
                {
                    "ok": True,
                    "organizationId": "org_northstar",
                    "windowDays": 7,
                    "alignment": {
                        "status": "YELLOW",
                        "badge": "🟡 Yellow",
                        "reasons": ["1 task has Rail 2 spoken claim only"],
                    },
                    "tasks": [
                        {
                            "id": "task_300",
                            "title": "Build MCP Server",
                            "status": "IN_PROGRESS",
                            "verificationTier": "TIER1_READY",
                            "verificationRail": "rail_1_direct_pr_merge",
                            "railClassification": "VERIFIED_PROOF_RAIL_3_5",
                            "treatAsDone": True,
                            "speakerName": "Sarah Chen",
                            "speakerQuote": "Merged PR #418 with full unit test coverage.",
                            "citations": [
                                {
                                    "id": "src_1",
                                    "factId": "fact_1",
                                    "sourceProvider": "github",
                                    "sourceObjectId": "pr_418",
                                    "sourceUrl": "https://github.com/northstar/web-app/pull/418",
                                }
                            ],
                        },
                        {
                            "id": "task_301",
                            "title": "Send renewal pricing deck",
                            "status": "TODO",
                            "verificationTier": "TIER2_CANDIDATE",
                            "verificationRail": "rail_4_debrief_transcript_proof",
                            "railClassification": "SPOKEN_CLAIM_RAIL_2",
                            "treatAsDone": False,
                            "speakerName": "Elena Rostova",
                            "speakerQuote": "I think the pricing deck went out this morning.",
                            "citations": [],
                        },
                    ],
                    "decisions": [
                        {
                            "id": "dec_resolved",
                            "title": "Token Encryption Standard",
                            "status": "DECIDED",
                            "doNotViolate": True,
                            "winningOption": {"id": "opt_hkdf", "label": "HKDF-SHA256"},
                            "criteria": [{"id": "c_1", "name": "Security", "weight": 10}],
                        }
                    ],
                    "resolvedDecisions": [
                        {
                            "id": "dec_resolved",
                            "title": "Token Encryption Standard",
                            "status": "DECIDED",
                            "doNotViolate": True,
                            "winningOption": {"id": "opt_hkdf", "label": "HKDF-SHA256"},
                            "criteria": [{"id": "c_1", "name": "Security", "weight": 10}],
                        }
                    ],
                    "pendingDecisions": [],
                    "activeLeases": [
                        {
                            "id": "lease_1",
                            "agentId": "claude-code",
                            "claimedGlobs": ["src/mcp/*"],
                            "worktreeBranch": "feat/task_300-mcp",
                        }
                    ],
                    "seams": [
                        {"id": "seam_1", "tag": "WIN", "summary": "Shipped context gate"}
                    ],
                    "orgBrief": {
                        "id": "brief_org_1",
                        "kind": "DAILY_BRIEF",
                        "summaryJson": {"headline": "Daily brief: 1 shipped"},
                    },
                    "teamBrief": {
                        "id": "brief_team_1",
                        "kind": "TEAM_ROLLUP",
                        "summaryJson": {"headline": "Team rollup: Platform Eng"},
                    },
                    "corrections": [
                        {
                            "id": "corr_1",
                            "authorName": "Sarah Chen",
                            "body": "Devon co-authored PR #418.",
                            "status": "APPLIED",
                            "hoursRemaining": 60,
                        }
                    ],
                }
            ).encode("utf-8")
        if "/api/v1/briefs" in url and req.get_method() == "GET":
            return 200, json.dumps(
                {
                    "ok": True,
                    "organizationId": "org_northstar",
                    "orgBrief": {"id": "brief_org_1", "kind": "DAILY_BRIEF"},
                    "teamBrief": {"id": "brief_team_1", "kind": "TEAM_ROLLUP"},
                    "briefs": [
                        {"id": "brief_org_1", "kind": "DAILY_BRIEF"},
                        {"id": "brief_team_1", "kind": "TEAM_ROLLUP"},
                    ],
                    "corrections": [
                        {"id": "corr_1", "body": "Devon co-authored PR #418.", "hoursRemaining": 60}
                    ],
                }
            ).encode("utf-8")
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
                    "notes": [{"topic": "MCP", "bodyMarkdown": "Resources + Prompts + Tools"}],
                }
            ).encode("utf-8")
        return 200, json.dumps({"ok": True, "seam": {"id": "seam_mcp_1"}}).encode("utf-8")

    client = BridgeClient(cfg, agent_id="claude-code", transport=mock_transport, env=env)
    return cfg, client


def test_mcp_schema_token_budget_under_900_tokens() -> None:
    """Verify the consolidated tools, 3 resources, and 3 prompts stay strictly under 900 estimated tokens."""
    assert len(MCP_RESOURCES) == 3
    assert len(MCP_PROMPTS) == 3
    assert len(MCP_TOOLS) == 8
    tool_names = [t["name"] for t in MCP_TOOLS]
    assert tool_names == [
        "bridge_context",
        "bridge_context_pull",
        "bridge_brief_latest",
        "bridge_seam",
        "bridge_decide",
        "bridge_scope",
        "bridge_job",
        "bridge_fact",
    ]
    serialized = json.dumps(MCP_TOOLS, separators=(",", ":"))
    estimated_tokens = len(serialized) // 4
    assert estimated_tokens < 900, f"Tool schema token count {estimated_tokens} exceeds 900-token budget"


def test_mcp_resources_prompts_and_eight_tools(tmp_path: Path) -> None:
    """Verify JSON-RPC 2.0 initialize, resources/read, prompts/get, and tools/call for all 8 tools."""
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

    # 4. tools/call across all 8 tools
    tool_calls = [
        ("bridge_context", {}),
        ("bridge_context_pull", {"days": 7, "team": "platform-eng"}),
        ("bridge_brief_latest", {"team": "platform-eng"}),
        ("bridge_seam", {"action": "log", "tag": "win", "summary": "MCP seam logged"}),
        ("bridge_decide", {"action": "propose", "title": "API Auth", "context": "Bearer vs mTLS"}),
        ("bridge_scope", {"action": "claim", "globs": ["src/mcp/*"], "ttl": "15m"}),
        ("bridge_job", {"action": "list"}),
        ("bridge_fact", {"statement": "MCP stdio server verified with 8 tools", "task_id": "task_300"}),
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
        if tool_name == "bridge_context_pull":
            assert payload["alignment"]["badge"] == "🟡 Yellow"
            assert len(payload["tasks"]) == 2
            assert payload["tasks"][0]["speakerQuote"] == "Merged PR #418 with full unit test coverage."
            assert payload["tasks"][1]["railClassification"] == "SPOKEN_CLAIM_RAIL_2"
            assert payload["tasks"][1]["treatAsDone"] is False
            assert payload["resolvedDecisions"][0]["winningOption"]["label"] == "HKDF-SHA256"
            assert payload["orgBrief"]["id"] == "brief_org_1"
            assert payload["teamBrief"]["id"] == "brief_team_1"
            assert len(payload["corrections"]) == 1
        if tool_name == "bridge_brief_latest":
            assert payload["orgBrief"]["id"] == "brief_org_1"
            assert payload["teamBrief"]["id"] == "brief_team_1"
            assert len(payload["corrections"]) == 1


def test_cli_bridge_context_and_briefs_commands(tmp_path: Path) -> None:
    """Verify `brg context --days 7 --team platform-eng --json` and `brg briefs --json` CLI execution."""
    cfg, client = _make_mcp_fixture(tmp_path)
    env = {
        "BRIDGE_API_KEY": "xrkey_v1.mcp_test_token",
        "BRIDGE_CREDENTIALS_PATH": str(tmp_path / "creds.json"),
    }

    out_ctx = io.StringIO()
    code_ctx = run_cli(
        ["context", "--days", "7", "--team", "platform-eng", "--json"],
        cwd=tmp_path,
        env=env,
        stdout_stream=out_ctx,
        transport=client._transport,
    )
    assert code_ctx == EXIT_OK
    ctx_payload = json.loads(out_ctx.getvalue())
    assert ctx_payload["ok"] is True
    assert ctx_payload["windowDays"] == 7
    assert ctx_payload["alignment"]["status"] == "YELLOW"
    assert len(ctx_payload["tasks"]) == 2
    assert len(ctx_payload["resolvedDecisions"]) == 1
    assert len(ctx_payload["activeLeases"]) == 1
    assert ctx_payload["orgBrief"]["id"] == "brief_org_1"

    out_briefs = io.StringIO()
    code_briefs = run_cli(
        ["briefs", "--team", "platform-eng", "--json"],
        cwd=tmp_path,
        env=env,
        stdout_stream=out_briefs,
        transport=client._transport,
    )
    assert code_briefs == EXIT_OK
    briefs_payload = json.loads(out_briefs.getvalue())
    assert briefs_payload["ok"] is True
    assert briefs_payload["orgBrief"]["id"] == "brief_org_1"
    assert briefs_payload["teamBrief"]["id"] == "brief_team_1"


def test_cross_agent_skills_manifests_and_session_start_hook() -> None:
    """Verify the 4 standard skills under skills/, dual marketplace manifests, and hooks/hooks.json."""
    repo_root = Path(__file__).resolve().parents[1]

    for skill_slug in ("bridge-context", "bridge-cli", "bridge-api", "bridge-updates"):
        skill_path = repo_root / "skills" / skill_slug / "SKILL.md"
        assert skill_path.is_file(), f"Missing {skill_path}"
        text = skill_path.read_text(encoding="utf-8")
        assert f"name: {skill_slug}" in text

    ctx_skill = (repo_root / "skills" / "bridge-context" / "SKILL.md").read_text(encoding="utf-8")
    assert "bridge_context_pull" in ctx_skill
    assert "Rail 2" in ctx_skill
    assert "Rail 3-5" in ctx_skill
    assert "🟢 Green" in ctx_skill
    assert "🟡 Yellow" in ctx_skill
    assert "🔴 Red" in ctx_skill

    claude_mp = json.loads((repo_root / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8"))
    claude_plugin = json.loads((repo_root / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    agents_mp = json.loads((repo_root / ".agents" / "plugins" / "marketplace.json").read_text(encoding="utf-8"))
    hooks_cfg = json.loads((repo_root / "hooks" / "hooks.json").read_text(encoding="utf-8"))

    assert claude_mp["plugins"][0]["name"] == "bridge"
    assert claude_plugin["hooks"] == "./hooks/hooks.json"
    assert agents_mp["plugins"][0]["name"] == "bridge"
    assert "SessionStart" in hooks_cfg["hooks"]
    hook_cmd = hooks_cfg["hooks"]["SessionStart"][0]["hooks"][0]["command"]
    assert "bridge_context_pull" in hook_cmd


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
    assert len(lines[1]["result"]["tools"]) == 8


def test_context_fallback_alignment_green_yellow_red_and_edge_cases(tmp_path: Path) -> None:
    """Verify fallback alignment computation (GREEN/YELLOW/RED), days boundary clamping, TTY human formatting, and error exit codes."""
    from bridge_plugin.commands.context import _compute_fallback_alignment, _format_context_human
    from bridge_plugin.runtime import EXIT_AUTH_MISSING

    # 1. GREEN when empty / clean
    green = _compute_fallback_alignment({"tasks": [], "decisions": [], "leases": [], "corrections": []})
    assert green["status"] == "GREEN"
    assert green["badge"] == "🟢 Green"

    # 2. YELLOW when Rail 2 spoken claim or active lease or 72h correction exists
    yellow = _compute_fallback_alignment(
        {
            "activeTasks": [{"id": "t_1", "status": "IN_PROGRESS", "verificationTier": "TIER2_CANDIDATE"}],
            "decisions": [{"id": "d_1", "status": "DECIDED"}],
            "activeLeases": [{"id": "l_1", "agentId": "cursor", "claimedGlobs": ["src/*"]}],
            "corrections": [{"id": "c_1", "body": "Correction note"}],
        }
    )
    assert yellow["status"] == "YELLOW"
    assert yellow["badge"] == "🟡 Yellow"
    assert any("Rail 2" in r for r in yellow["reasons"])

    # 3. RED when task is BLOCKED_ON_DECISION or HIGH/CRITICAL decision is open
    red = _compute_fallback_alignment(
        {
            "activeTasks": [{"id": "t_blocked", "status": "BLOCKED_ON_DECISION"}],
            "pendingDecisions": [{"id": "d_crit", "status": "OPEN", "urgency": "CRITICAL"}],
        }
    )
    assert red["status"] == "RED"
    assert red["badge"] == "🔴 Red"
    assert any("t_blocked" in r for r in red["reasons"])

    # 4. Human formatting renders alignment badge, tasks, speaker quotes, decisions, leases, and briefs
    human_lines = _format_context_human(
        {
            "windowDays": 7,
            "organizationId": "org_northstar",
            "repository": "northstar/web-app",
            "gitBranch": "main",
            "team": {"name": "Platform Eng", "slug": "platform-eng"},
            "alignment": yellow,
            "resolvedDecisions": [
                {
                    "id": "dec_1",
                    "title": "Auth Scheme",
                    "winningOption": {"label": "PKCE"},
                    "criteria": [{"name": "Security"}],
                }
            ],
            "pendingDecisions": [{"id": "dec_2", "title": "Cache TTL", "urgency": "HIGH", "status": "OPEN"}],
            "tasks": [
                {
                    "id": "t_1",
                    "title": "Ship OAuth",
                    "status": "IN_PROGRESS",
                    "railTierLabel": "Rail 2 (Spoken Claim)",
                    "speakerName": "Sarah",
                    "speakerQuote": "We discussed shipping OAuth today.",
                    "citations": [{"sourceProvider": "granola", "sourceObjectId": "note_1"}],
                }
            ],
            "activeLeases": [{"agentId": "claude-code", "worktreeBranch": "feat/oauth", "claimedGlobs": ["src/auth/*"]}],
            "orgBrief": {"summaryJson": {"headline": "Org Headline"}},
            "teamBrief": {"summaryJson": {"headline": "Team Headline"}},
            "corrections": [{"status": "APPLIED", "authorName": "Devon", "body": "Fixed PR number"}],
        }
    )
    rendered = "\n".join(human_lines)
    assert "🟡 Yellow" in rendered
    assert "[DECIDED — DO NOT VIOLATE] dec_1" in rendered
    assert "Sarah: We discussed shipping OAuth today." in rendered
    assert "[ORG Brief] Org Headline" in rendered
    assert "[TEAM Brief] Team Headline" in rendered

    # 5. Days boundary clamping (--days 99 -> 30) and 401 error propagation
    captured_urls: list[str] = []

    def clamp_transport(req: Any, _timeout: float) -> tuple[int, bytes]:
        captured_urls.append(req.full_url)
        return 200, json.dumps({"ok": True, "tasks": []}).encode("utf-8")

    env = {
        "BRIDGE_API_KEY": "xrkey_v1.mcp_test_token",
        "BRIDGE_CREDENTIALS_PATH": str(tmp_path / "creds.json"),
    }
    out_clamp = io.StringIO()
    code_clamp = run_cli(
        ["context", "--days", "99", "--json"],
        cwd=tmp_path,
        env=env,
        stdout_stream=out_clamp,
        transport=clamp_transport,
    )
    assert code_clamp == EXIT_OK
    assert "days=30" in captured_urls[0]
    assert json.loads(out_clamp.getvalue())["windowDays"] == 30

    out_err = io.StringIO()
    code_err = run_cli(
        ["context", "--json"],
        cwd=tmp_path,
        env=env,
        stdout_stream=out_err,
        transport=lambda _req, _timeout: (401, b'{"ok": false, "error": "UNAUTHORIZED"}'),
    )
    assert code_err == EXIT_AUTH_MISSING

