"""Phase 11 Challenger Adversarial Test Suite for bridge-plugin.

Exercises:
1. Deeply nested child task blocker gate (immediate parent vs ancestor BLOCKED_ON_DECISION).
2. Deeply nested context steering rationale propagation (both JSON non-TTY and Human TTY modes).
3. Token budget and clean-room zero forbidden tokens guardrails.
"""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any
import pytest

from bridge_plugin.cli import run_cli
from bridge_plugin.mcp_server import MCP_TOOLS
from bridge_plugin.runtime import EXIT_CONCURRENCY_BLOCK, EXIT_OK


class NonTTYBuffer(io.StringIO):
    def isatty(self) -> bool:
        return False


class TTYBuffer(io.StringIO):
    def isatty(self) -> bool:
        return True


def _setup_env(tmp_path: Path) -> dict[str, str]:
    (tmp_path / ".bridge.json").write_text(
        json.dumps(
            {
                "plugin": "bridge",
                "organizationId": "org_adversarial",
                "apiUrl": "https://bridge.xray.tech",
                "repository": "adversarial/test-repo",
            }
        ),
        encoding="utf-8",
    )
    return {
        "BRIDGE_API_KEY": "xrkey_v1.adversarial_test_key",
        "BRIDGE_CREDENTIALS_PATH": str(tmp_path / "creds.json"),
    }


def test_tasks_start_blocks_when_immediate_parent_is_blocked_on_decision(tmp_path: Path) -> None:
    """Verify tasks start exits with code 2 when parent is BLOCKED_ON_DECISION."""
    env = _setup_env(tmp_path)

    def mock_tasks_transport(_req: Any, _timeout: float) -> tuple[int, bytes]:
        return 200, json.dumps(
            {
                "ok": True,
                "tasks": [
                    {
                        "id": "task_gp",
                        "title": "Grandparent",
                        "status": "BLOCKED_ON_DECISION",
                    },
                    {
                        "id": "task_p",
                        "title": "Parent",
                        "status": "BLOCKED_ON_DECISION",
                        "parentTaskId": "task_gp",
                    },
                    {
                        "id": "task_child",
                        "title": "Child",
                        "status": "TODO",
                        "parentTaskId": "task_p",
                    },
                ],
            }
        ).encode("utf-8")

    out = NonTTYBuffer()
    code = run_cli(
        ["tasks", "start", "task_child", "--no-checkout"],
        cwd=tmp_path,
        env=env,
        stdout_stream=out,
        transport=mock_tasks_transport,
    )
    assert code == EXIT_CONCURRENCY_BLOCK
    assert "BLOCKED" in out.getvalue()


def test_tasks_start_blocks_when_task_itself_is_blocked_on_decision(tmp_path: Path) -> None:
    """Verify tasks start exits with code 2 when task itself has status BLOCKED_ON_DECISION."""
    env = _setup_env(tmp_path)

    def mock_tasks_transport(_req: Any, _timeout: float) -> tuple[int, bytes]:
        return 200, json.dumps(
            {
                "ok": True,
                "tasks": [
                    {
                        "id": "task_deep_grandchild",
                        "title": "Deep Grandchild",
                        "status": "BLOCKED_ON_DECISION",
                        "parentTaskId": "task_child",
                    },
                ],
            }
        ).encode("utf-8")

    out = NonTTYBuffer()
    code = run_cli(
        ["tasks", "start", "task_deep_grandchild", "--no-checkout"],
        cwd=tmp_path,
        env=env,
        stdout_stream=out,
        transport=mock_tasks_transport,
    )
    assert code == EXIT_CONCURRENCY_BLOCK
    assert "BLOCKED" in out.getvalue()


def test_context_surfaces_deeply_nested_steering_rationale_json(tmp_path: Path) -> None:
    """Verify brg context in non-TTY mode includes steering rationale in JSON payload."""
    env = _setup_env(tmp_path)
    steering_guidance = "Steer: Mandate ChaCha20-Poly1305 for all edge routes."

    def mock_context_transport(_req: Any, _timeout: float) -> tuple[int, bytes]:
        return 200, json.dumps(
            {
                "ok": True,
                "tasks": [
                    {
                        "id": "task_deep_grandchild",
                        "title": "Grandchild Micro-Service Config",
                        "status": "TODO",
                        "parentTaskId": "task_child",
                        "parentSteeringRationale": steering_guidance,
                    }
                ],
                "decisions": [],
                "leases": [],
                "seams": [],
                "alignment": {"status": "GREEN", "badge": "🟢 Green", "reasons": []},
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
    assert steering_guidance in content


def test_context_surfaces_deeply_nested_steering_rationale_human_tty(tmp_path: Path) -> None:
    """Verify brg context in TTY mode renders '↳ Steering Rationale (from Parent)'."""
    env = _setup_env(tmp_path)
    steering_guidance = "Steer: Mandate ChaCha20-Poly1305 for all edge routes."

    def mock_context_transport(_req: Any, _timeout: float) -> tuple[int, bytes]:
        return 200, json.dumps(
            {
                "ok": True,
                "tasks": [
                    {
                        "id": "task_deep_grandchild",
                        "title": "Grandchild Micro-Service Config",
                        "status": "TODO",
                        "parentTaskId": "task_child",
                        "parentSteeringRationale": steering_guidance,
                    }
                ],
                "decisions": [],
                "leases": [],
                "seams": [],
                "alignment": {"status": "GREEN", "badge": "🟢 Green", "reasons": []},
            }
        ).encode("utf-8")

    out = TTYBuffer()
    code = run_cli(
        ["context"],
        cwd=tmp_path,
        env=env,
        stdout_stream=out,
        transport=mock_context_transport,
    )
    assert code == EXIT_OK
    content = out.getvalue()
    assert steering_guidance in content
    assert "↳ Steering Rationale (from Parent)" in content


def test_mcp_tools_consolidated_exact_eight() -> None:
    """Assert exactly 8 consolidated MCP tools exist."""
    assert len(MCP_TOOLS) == 8


def test_mcp_schema_strict_token_budget() -> None:
    """Assert MCP tools schema serializes under 900 tokens (~4 chars per token)."""
    serialized = json.dumps(MCP_TOOLS, separators=(",", ":"))
    token_count = len(serialized) // 4
    assert token_count < 900, f"Token count {token_count} exceeds 900"


def test_clean_room_strict_forbidden_tokens() -> None:
    """Assert zero forbidden tokens exist in bridge_plugin source."""
    root = Path(__file__).resolve().parent.parent / "src" / "bridge_plugin"
    forbidden = [
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
    for py_file in root.rglob("*.py"):
        content = py_file.read_text(encoding="utf-8").lower()
        for tok in forbidden:
            assert tok.lower() not in content, f"Forbidden token {tok} found in {py_file}"
