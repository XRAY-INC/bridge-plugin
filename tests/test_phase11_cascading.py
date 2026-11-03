"""Phase 11 Cascading Task Hierarchy, Blocker Gates & MCP Budget Tests.

Requirements Covered:
- R4: Multi-Task Blocking & Parent/Child Cascading across bridge-plugin:
  1. `brg tasks create --parent <id>` CLI flag parses and forwards parentTaskId / externalParentRef.
  2. `brg tasks start` prevents starting child tasks whose parent is BLOCKED_ON_DECISION (exit code 2).
  3. `brg context` surfaces parent task steering rationale for child tasks.
- R5: Architecture Guardrails in bridge-plugin:
  4. Exactly 8 consolidated MCP tools in `MCP_TOOLS`.
  5. MCP tools token budget under 900 tokens.
  6. Clean-room verification: zero forbidden tokens (tpx, tailscale, tail3ef763, smb://, tree_sitter, ed25519, recXXXX, airtable).
"""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

import pytest

from bridge_plugin.cli import run_cli
from bridge_plugin.config import resolve_config
from bridge_plugin.mcp_server import MCP_TOOLS
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
        "BRIDGE_API_KEY": "xrkey_v1.phase11_test_key",
        "BRIDGE_CREDENTIALS_PATH": str(tmp_path / "creds.json"),
    }


# ============================================================================
# Tier 1 & Tier 2: CLI Tasks Parent/Child Hierarchy & Blocker Gates
# ============================================================================

def test_tasks_create_accepts_parent_flag_and_forwards_payload(tmp_path: Path) -> None:
    """Verify `brg tasks create --parent <id>` forwards parentTaskId / externalParentRef."""
    env = _setup_env(tmp_path)
    captured_payloads: list[dict[str, Any]] = []

    def mock_transport(req: Any, _timeout: float) -> tuple[int, bytes]:
        if req.data:
            captured_payloads.append(json.loads(req.data.decode("utf-8")))
        return 200, json.dumps(
            {
                "ok": True,
                "task": {
                    "id": "task_child_101",
                    "title": "Subtask Auth Handshake",
                    "status": "TODO",
                    "parentTaskId": "task_parent_99",
                    "externalParentRef": "task_parent_99",
                },
            }
        ).encode("utf-8")

    out = NonTTYBuffer()
    code = run_cli(
        ["tasks", "create", "Subtask Auth Handshake", "--parent", "task_parent_99", "--json"],
        cwd=tmp_path,
        env=env,
        stdout_stream=out,
        transport=mock_transport,
    )
    assert code == EXIT_OK
    assert len(captured_payloads) == 1
    body = captured_payloads[0]
    assert body.get("parentTaskId") == "task_parent_99" or body.get("externalParentRef") == "task_parent_99"
    assert body.get("title") == "Subtask Auth Handshake"


def test_tasks_start_blocks_child_task_when_parent_is_blocked_on_decision(tmp_path: Path) -> None:
    """Verify `brg tasks start` exits with code 2 if the task's parent has status BLOCKED_ON_DECISION."""
    env = _setup_env(tmp_path)

    # In this mock, child task itself is TODO, but parent task is BLOCKED_ON_DECISION
    def mock_tasks_transport(_req: Any, _timeout: float) -> tuple[int, bytes]:
        return 200, json.dumps(
            {
                "ok": True,
                "tasks": [
                    {
                        "id": "task_parent_blocked",
                        "title": "Architectural Gateway Redesign",
                        "status": "BLOCKED_ON_DECISION",
                        "decisionRationale": "Awaiting security council signoff",
                    },
                    {
                        "id": "task_child_subtask",
                        "title": "Implement PKCE Code Verifier",
                        "status": "TODO",
                        "parentTaskId": "task_parent_blocked",
                    },
                    {
                        "id": "task_standalone_todo",
                        "title": "Update Documentation",
                        "status": "TODO",
                        "parentTaskId": None,
                    },
                ],
            }
        ).encode("utf-8")

    # Starting the child task whose parent is BLOCKED_ON_DECISION must exit with code 2
    out_child = NonTTYBuffer()
    code_child = run_cli(
        ["tasks", "start", "task_child_subtask", "--no-checkout"],
        cwd=tmp_path,
        env=env,
        stdout_stream=out_child,
        transport=mock_tasks_transport,
    )
    assert code_child == EXIT_CONCURRENCY_BLOCK
    child_output = out_child.getvalue()
    assert (
        "BLOCKED" in child_output
        or "BLOCKED_ON_DECISION" in child_output
        or "parent" in child_output.lower()
    )

    # Starting a standalone task with no blocked parent succeeds with code 0
    out_standalone = NonTTYBuffer()
    code_standalone = run_cli(
        ["tasks", "start", "task_standalone_todo", "--no-checkout"],
        cwd=tmp_path,
        env=env,
        stdout_stream=out_standalone,
        transport=mock_tasks_transport,
    )
    assert code_standalone == EXIT_OK


def test_context_surfaces_parent_steering_rationale_for_child_tasks(tmp_path: Path) -> None:
    """Verify `brg context` extracts and displays parent steering rationale for child tasks."""
    env = _setup_env(tmp_path)

    parent_steering_note = "Steer: Deploy microgateway first; restrict to 5 pilot tenants."

    def mock_context_transport(_req: Any, _timeout: float) -> tuple[int, bytes]:
        return 200, json.dumps(
            {
                "ok": True,
                "tasks": [
                    {
                        "id": "task_child_proxy",
                        "title": "Configure Proxy Gateway Rules",
                        "status": "TODO",
                        "parentTaskId": "task_parent_arch",
                        "parentSteeringRationale": parent_steering_note,
                        "parentTask": {
                            "id": "task_parent_arch",
                            "title": "Gateway Migration",
                            "decisionRationale": parent_steering_note,
                        },
                    }
                ],
                "decisions": [],
                "leases": [],
                "seams": [],
                "alignment": {
                    "level": "GREEN",
                    "reasons": ["Task aligned with active architectural decisions"],
                },
            }
        ).encode("utf-8")

    out = NonTTYBuffer()
    code = run_cli(
        ["context"],
        cwd=tmp_path,
        env=env,
        stdout_stream=out,
        transport=mock_context_transport,
    )
    assert code == EXIT_OK
    content = out.getvalue()
    assert (
        parent_steering_note in content
        or "Steer:" in content
        or "Steering Rationale" in content
        or "microgateway" in content
    )


# ============================================================================
# Tier 1 & Tier 5: R5 Guardrails & Token Budget Verification
# ============================================================================

def test_mcp_tools_exact_count_eight() -> None:
    """R5 Guardrail: Exactly 8 consolidated MCP tools exist in MCP_TOOLS."""
    expected_tools = {
        "bridge_context",
        "bridge_context_pull",
        "bridge_brief_latest",
        "bridge_seam",
        "bridge_decide",
        "bridge_scope",
        "bridge_job",
        "bridge_fact",
    }
    actual_names = {t["name"] for t in MCP_TOOLS}
    assert len(MCP_TOOLS) == 8, f"Expected exactly 8 MCP tools, found {len(MCP_TOOLS)}"
    assert actual_names == expected_tools


def test_mcp_schema_token_budget_strictly_under_900_tokens() -> None:
    """R5 Guardrail: Full MCP tool schema serializes strictly under 900 estimated tokens."""
    serialized = json.dumps(MCP_TOOLS, separators=(",", ":"))
    # Approximation standard across Bridge tests: 4 chars per token
    token_estimate = len(serialized) // 4
    assert token_estimate < 900, (
        f"MCP tool schemas exceed 900 token limit: {token_estimate} tokens ({len(serialized)} chars)"
    )


def test_clean_room_zero_forbidden_tokens() -> None:
    """R5 Guardrail: No file in src/bridge_plugin/*.py may contain forbidden legacy tokens."""
    root = Path(__file__).resolve().parent.parent / "src" / "bridge_plugin"
    assert root.exists() and root.is_dir()

    forbidden_tokens = [
        "tpx",
        "tailscale",
        "tail3ef763",
        "smb://",
        "tree_sitter",
        "tree-sitter",
        "ed25519",
        "recXXXX",
        "airtable",
    ]

    py_files = list(root.rglob("*.py"))
    assert len(py_files) >= 8

    for py_file in py_files:
        content = py_file.read_text(encoding="utf-8").lower()
        for token in forbidden_tokens:
            assert token.lower() not in content, f"Forbidden legacy token '{token}' found in {py_file}"
