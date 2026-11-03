"""Stdio JSON-RPC 2.0 MCP Server for Bridge: 3 Resources, 3 Prompts, and 6 Consolidated Tools (<900 schema tokens)."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Mapping

from . import __version__
from .client import BridgeClient
from .commands.concurrency import (
    handle_claim,
    handle_claims_list,
    handle_release,
)
from .commands.context import (
    handle_brief_latest,
    handle_context_pull,
)
from .commands.decide import (
    handle_decide_check,
    handle_decide_list,
    handle_decide_propose,
    handle_decide_resolve,
    handle_decide_score,
    handle_decide_show,
)
from .commands.seam import (
    handle_seam_edit,
    handle_seam_list,
    handle_seam_log,
    handle_seam_retract,
)
from .commands.tasks import (
    handle_jobs_claim,
    handle_jobs_complete,
    handle_jobs_dispatch,
    handle_jobs_list,
    handle_tasks_capsule,
    handle_tasks_verify,
)
from .config import (
    BridgeConfig,
    get_branch_state,
    load_local_claims,
    resolve_config,
)
from .runtime import (
    EXIT_OK,
    detect_git_branch,
    detect_runtime_context,
)

MCP_PROTOCOL_VERSION = "2024-11-05"

# ---------------------------------------------------------------------------
# 1. Three URI-Addressed MCP Resources
# ---------------------------------------------------------------------------

MCP_RESOURCES: list[dict[str, Any]] = [
    {
        "uri": "bridge://context",
        "name": "Bridge Workspace Context",
        "description": "Unified snapshot of repo binding, active branch task/job, scope leases, open decisions, and scratchpad notes.",
        "mimeType": "application/json",
    },
    {
        "uri": "bridge://decisions/open",
        "name": "Open 1-Click Decisions",
        "description": "Open decision cards with weighted criteria and option rankings.",
        "mimeType": "application/json",
    },
    {
        "uri": "bridge://tasks/active",
        "name": "Active Tasks & Queued Jobs",
        "description": "Active workspace tasks and queued agent execution capsules.",
        "mimeType": "application/json",
    },
]

# ---------------------------------------------------------------------------
# 2. Three Parameterized MCP Prompts
# ---------------------------------------------------------------------------

MCP_PROMPTS: list[dict[str, Any]] = [
    {
        "name": "bridge-session-brief",
        "description": "Hydrate an agent at session boot with Bridge repo context, branch state, claims, and open decisions.",
        "arguments": [
            {"name": "repository", "description": "Optional repository slug override", "required": False},
            {"name": "branch", "description": "Optional git branch override", "required": False},
        ],
    },
    {
        "name": "bridge-escalate-decision",
        "description": "Structured trade-off template for proposing a 1-Click Decision Card in Bridge.",
        "arguments": [
            {"name": "title", "description": "Short decision title", "required": True},
            {"name": "tradeoff", "description": "Core architectural or product trade-off summary", "required": True},
            {"name": "blocked_task_id", "description": "Optional task ID blocked by this decision", "required": False},
        ],
    },
    {
        "name": "bridge-turn-handoff",
        "description": "Structured turn-closure handoff template for releasing scope claims and updating the Bridge scratchpad.",
        "arguments": [
            {"name": "summary", "description": "Summary of what shipped or changed in this turn", "required": True},
            {"name": "next_steps", "description": "Open threads or next steps for the next agent/human", "required": False},
        ],
    },
]

# ---------------------------------------------------------------------------
# 3. Eight Consolidated Action Tools (< 900 total schema tokens)
# ---------------------------------------------------------------------------

MCP_TOOLS: list[dict[str, Any]] = [
    {
        "name": "bridge_context",
        "description": "Read or update Bridge status, branch binding, claims, and scratchpad.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "topic": {"type": "string"},
                "write_topic": {"type": "string"},
                "write_body": {"type": "string"},
            },
        },
    },
    {
        "name": "bridge_context_pull",
        "description": "Pull 7-day org context: tasks, quotes, citations, decisions, leases, seams, briefs & 72h corrections.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "days": {"type": "integer", "default": 7},
                "team": {"type": "string"},
                "repo": {"type": "string"},
            },
        },
    },
    {
        "name": "bridge_brief_latest",
        "description": "Fetch latest ORG/TEAM Morning Briefs and 72h Corrections.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "team": {"type": "string"},
                "kind": {"type": "string", "enum": ["DAILY_BRIEF", "ACCOMPLISHMENT_DIGEST", "TEAM_ROLLUP"]},
            },
        },
    },
    {
        "name": "bridge_seam",
        "description": "Log, edit, retract, or list seams (win, decision, friction, handoff).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["log", "edit", "retract", "list"], "default": "log"},
                "tag": {"type": "string", "enum": ["win", "decision", "friction", "handoff"], "default": "win"},
                "summary": {"type": "string"},
                "seam_id": {"type": "string"},
                "task_id": {"type": "string"},
            },
        },
    },
    {
        "name": "bridge_decide",
        "description": "List, propose, score, resolve, or check 1-Click Decisions.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["list", "propose", "score", "resolve", "status"],
                    "default": "list",
                },
                "decision_id": {"type": "string"},
                "title": {"type": "string"},
                "context": {"type": "string"},
                "urgency": {"type": "string", "enum": ["CRITICAL", "HIGH", "STANDARD", "LOW"]},
                "options": {"type": "string"},
                "criteria": {"type": "string"},
                "option": {"type": "string"},
                "criterion": {"type": "string"},
                "score": {"type": "integer", "minimum": 1, "maximum": 10},
                "rationale": {"type": "string"},
                "task_id": {"type": "string"},
            },
        },
    },
    {
        "name": "bridge_scope",
        "description": "Claim, release, or list cross-worktree file scope leases.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["claim", "release", "list"], "default": "list"},
                "globs": {"type": "array", "items": {"type": "string"}},
                "ttl": {"type": "string"},
                "reason": {"type": "string"},
                "lease_id": {"type": "string"},
                "all": {"type": "boolean"},
            },
        },
    },
    {
        "name": "bridge_job",
        "description": "List, dispatch, claim, complete, or compile capsules for Bridge agent jobs.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["list", "dispatch", "claim", "complete", "capsule"], "default": "list"},
                "job_id": {"type": "string"},
                "task_id": {"type": "string"},
                "globs": {"type": "string"},
                "pr_url": {"type": "string"},
                "commit_sha": {"type": "string"},
                "notes": {"type": "string"},
            },
        },
    },
    {
        "name": "bridge_fact",
        "description": "Ingest a bounded (<=280 char) verification fact for task reconciliation.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "statement": {"type": "string"},
                "task_id": {"type": "string"},
                "kind": {
                    "type": "string",
                    "enum": ["OUTCOME", "DECISION", "COMMITMENT", "BLOCKER", "ACCOMPLISHMENT"],
                    "default": "OUTCOME",
                },
                "confidence": {"type": "integer", "minimum": 1, "maximum": 100, "default": 90},
            },
            "required": ["statement"],
        },
    },
]


class _StringBuffer:
    def __init__(self) -> None:
        self.parts: list[str] = []

    def write(self, text: str) -> None:
        self.parts.append(text)

    def flush(self) -> None:
        pass

    def isatty(self) -> bool:
        return False

    def get_json(self) -> dict[str, Any]:
        raw = "".join(self.parts).strip()
        if not raw:
            return {"ok": True}
        try:
            return json.loads(raw)
        except Exception:
            return {"ok": False, "raw": raw}


def build_context_snapshot(config: BridgeConfig, client: BridgeClient) -> dict[str, Any]:
    """Assemble the unified bridge://context payload."""
    runtime = detect_runtime_context(cwd=config.workspace_root)
    branch = detect_git_branch(cwd=config.workspace_root)
    branch_state = get_branch_state(config, branch=branch)
    local_claims = load_local_claims(config)
    spool_entries = client.read_spool()

    open_decisions: list[Any] = []
    scratchpad_notes: list[Any] = []
    remote_leases: list[Any] = []

    if config.is_authenticated or config.is_bound:
        dec_resp = client.request("GET", "/api/v1/decide", params={"status": "OPEN"}, require_auth=False)
        if dec_resp.ok and isinstance(dec_resp.data.get("decisions"), list):
            open_decisions = dec_resp.data["decisions"]

        sp_params = {"repository": config.repository} if config.repository else {}
        sp_resp = client.request("GET", "/api/v1/fleet/scratchpad", params=sp_params, require_auth=False)
        if sp_resp.ok and isinstance(sp_resp.data.get("notes"), list):
            scratchpad_notes = sp_resp.data["notes"]

        lease_resp = client.request("GET", "/api/v1/fleet/leases", params=sp_params, require_auth=False)
        if lease_resp.ok and isinstance(lease_resp.data.get("leases"), list):
            remote_leases = lease_resp.data["leases"]

    return {
        "ok": True,
        "uri": "bridge://context",
        "organizationId": config.organization_id,
        "repository": config.repository,
        "apiUrl": config.api_url,
        "authenticated": config.is_authenticated,
        "gitBranch": branch,
        "branchState": branch_state,
        "runtime": runtime,
        "activeClaims": local_claims or remote_leases,
        "openDecisions": open_decisions,
        "scratchpadNotes": scratchpad_notes,
        "offlineSpoolCount": len(spool_entries),
    }


def read_mcp_resource(uri: str, config: BridgeConfig, client: BridgeClient) -> dict[str, Any]:
    """Resolve one of the 3 URI-addressed MCP resources."""
    if uri == "bridge://context":
        data = build_context_snapshot(config, client)
    elif uri == "bridge://decisions/open":
        resp = client.request("GET", "/api/v1/decide", params={"status": "OPEN"}, require_auth=False)
        data = {
            "ok": resp.ok,
            "uri": uri,
            "decisions": resp.data.get("decisions", []),
        }
    elif uri == "bridge://tasks/active":
        tasks_resp = client.request("GET", "/api/v1/tasks", require_auth=False)
        jobs_resp = client.request("GET", "/api/v1/fleet/jobs", require_auth=False)
        data = {
            "ok": tasks_resp.ok or jobs_resp.ok,
            "uri": uri,
            "tasks": tasks_resp.data.get("tasks", []),
            "jobs": jobs_resp.data.get("jobs", []),
        }
    else:
        raise ValueError(f"Unknown resource URI: {uri}")

    return {
        "contents": [
            {
                "uri": uri,
                "mimeType": "application/json",
                "text": json.dumps(data, indent=2, default=str),
            }
        ]
    }


def get_mcp_prompt(
    name: str,
    arguments: Mapping[str, Any] | None,
    config: BridgeConfig,
    client: BridgeClient,
) -> dict[str, Any]:
    """Render one of the 3 parameterized MCP prompts."""
    args = dict(arguments or {})
    if name == "bridge-session-brief":
        snapshot = build_context_snapshot(config, client)
        return {
            "description": "Bridge session boot brief",
            "messages": [
                {
                    "role": "user",
                    "content": {
                        "type": "text",
                        "text": (
                            "You are operating in a repository connected to Bridge.\n"
                            "Review the current workspace context before editing files:\n\n"
                            f"```json\n{json.dumps(snapshot, indent=2, default=str)}\n```"
                        ),
                    },
                }
            ],
        }
    if name == "bridge-escalate-decision":
        title = args.get("title") or "Architectural Trade-Off"
        tradeoff = args.get("tradeoff") or "Describe options and weighted criteria"
        blocked_task = args.get("blocked_task_id") or get_branch_state(config).get("taskId")
        return {
            "description": f"Escalate decision: {title}",
            "messages": [
                {
                    "role": "user",
                    "content": {
                        "type": "text",
                        "text": (
                            f"Propose a 1-Click Decision Card in Bridge using tool `bridge_decide` (action='propose'):\n"
                            f"- Title: {title}\n"
                            f"- Trade-off Context: {tradeoff}\n"
                            f"- Blocked Task ID: {blocked_task or '(none)'}\n"
                            "Include 2-3 clear options and weighted evaluation criteria (1-10)."
                        ),
                    },
                }
            ],
        }
    if name == "bridge-turn-handoff":
        summary = args.get("summary") or "Completed implementation step"
        next_steps = args.get("next_steps") or "Run verification suite"
        return {
            "description": "Bridge turn-closure handoff",
            "messages": [
                {
                    "role": "user",
                    "content": {
                        "type": "text",
                        "text": (
                            "Close this coding turn cleanly in Bridge:\n"
                            "1. Call `bridge_scope` with `action='release', all=true`.\n"
                            f"2. Call `bridge_seam` with `action='log', tag='handoff', summary='{summary} — Next: {next_steps}'`."
                        ),
                    },
                }
            ],
        }
    raise ValueError(f"Unknown prompt name: {name}")


def call_mcp_tool_handler(
    name: str,
    arguments: Mapping[str, Any] | None,
    config: BridgeConfig,
    client: BridgeClient,
) -> dict[str, Any]:
    """Execute one of the 6 consolidated MCP tools and return an MCP CallToolResult dict."""
    args_dict = dict(arguments or {})
    buf = _StringBuffer()

    if name == "bridge_context":
        if args_dict.get("write_topic") and args_dict.get("write_body"):
            from .commands.memory import handle_scratchpad_write

            ns = argparse.Namespace(
                topic=args_dict["write_topic"],
                body=args_dict["write_body"],
                repo=args_dict.get("repo"),
                branch=args_dict.get("branch"),
            )
            code = handle_scratchpad_write(ns, config, client, force_json=True, stdout_stream=buf)
            result_data = buf.get_json()
        else:
            result_data = build_context_snapshot(config, client)
            code = EXIT_OK

    elif name == "bridge_context_pull":
        ns = argparse.Namespace(
            days=args_dict.get("days", 7),
            team=args_dict.get("team"),
            repo=args_dict.get("repo") or args_dict.get("repository"),
        )
        code = handle_context_pull(ns, config, client, force_json=True, stdout_stream=buf)
        result_data = buf.get_json()

    elif name == "bridge_brief_latest":
        ns = argparse.Namespace(
            team=args_dict.get("team"),
            kind=args_dict.get("kind"),
        )
        code = handle_brief_latest(ns, config, client, force_json=True, stdout_stream=buf)
        result_data = buf.get_json()

    elif name == "bridge_seam":
        action = str(args_dict.get("action") or "log").lower()
        if action == "list":
            ns = argparse.Namespace(
                repo=args_dict.get("repo"),
                tag=args_dict.get("tag"),
                task=args_dict.get("task_id"),
                limit=args_dict.get("limit", 20),
            )
            code = handle_seam_list(ns, config, client, force_json=True, stdout_stream=buf)
        elif action == "edit":
            ns = argparse.Namespace(
                seam_id=args_dict.get("seam_id") or "last",
                summary=args_dict.get("summary") or "",
                tag=args_dict.get("tag"),
            )
            code = handle_seam_edit(ns, config, client, force_json=True, stdout_stream=buf)
        elif action == "retract":
            ns = argparse.Namespace(seam_id=args_dict.get("seam_id") or "last")
            code = handle_seam_retract(ns, config, client, force_json=True, stdout_stream=buf)
        else:
            ns = argparse.Namespace(
                tag=args_dict.get("tag") or "win",
                summary=args_dict.get("summary") or "",
                task=args_dict.get("task_id"),
                job=args_dict.get("job_id"),
                branch=args_dict.get("branch"),
                commit=args_dict.get("commit_sha"),
                repo=args_dict.get("repo"),
                agent=args_dict.get("agent"),
                confidence=args_dict.get("confidence"),
            )
            code = handle_seam_log(ns, config, client, force_json=True, stdout_stream=buf)
        result_data = buf.get_json()

    elif name == "bridge_decide":
        action = str(args_dict.get("action") or "list").lower()
        if action == "propose":
            ns = argparse.Namespace(
                title=args_dict.get("title") or "",
                context=args_dict.get("context") or "",
                urgency=args_dict.get("urgency") or "STANDARD",
                options=args_dict.get("options"),
                criteria=args_dict.get("criteria"),
                task=args_dict.get("task_id"),
                job=args_dict.get("job_id"),
            )
            code = handle_decide_propose(ns, config, client, force_json=True, stdout_stream=buf)
        elif action == "score":
            ns = argparse.Namespace(
                decision_id=args_dict.get("decision_id") or "",
                option=args_dict.get("option") or "0",
                criterion=args_dict.get("criterion") or "0",
                score=args_dict.get("score") or 5,
                rationale=args_dict.get("rationale"),
                weight=args_dict.get("weight"),
            )
            code = handle_decide_score(ns, config, client, force_json=True, stdout_stream=buf)
        elif action == "resolve":
            ns = argparse.Namespace(
                decision_id=args_dict.get("decision_id") or "",
                option=args_dict.get("option") or "",
                rationale=args_dict.get("rationale"),
            )
            code = handle_decide_resolve(ns, config, client, force_json=True, stdout_stream=buf)
        elif action == "status":
            if args_dict.get("decision_id"):
                ns = argparse.Namespace(decision_id=args_dict["decision_id"])
                code = handle_decide_show(ns, config, client, force_json=True, stdout_stream=buf)
            else:
                ns = argparse.Namespace(task=args_dict.get("task_id"))
                code = handle_decide_check(ns, config, client, force_json=True, stdout_stream=buf)
        else:
            ns = argparse.Namespace(
                status=args_dict.get("status"),
                urgency=args_dict.get("urgency"),
                task=args_dict.get("task_id"),
                limit=args_dict.get("limit", 25),
            )
            code = handle_decide_list(ns, config, client, force_json=True, stdout_stream=buf)
        result_data = buf.get_json()

    elif name == "bridge_scope":
        action = str(args_dict.get("action") or "list").lower()
        if action == "claim":
            globs = args_dict.get("globs") or []
            if isinstance(globs, str):
                globs = [globs]
            ns = argparse.Namespace(
                globs=globs,
                ttl=args_dict.get("ttl"),
                reason=args_dict.get("reason"),
                branch=args_dict.get("branch"),
                repo=args_dict.get("repo"),
                agent=args_dict.get("agent"),
            )
            code = handle_claim(ns, config, client, force_json=True, stdout_stream=buf)
        elif action == "release":
            ns = argparse.Namespace(
                lease_id=args_dict.get("lease_id"),
                all=bool(args_dict.get("all", False)),
                repo=args_dict.get("repo"),
                agent=args_dict.get("agent"),
            )
            code = handle_release(ns, config, client, force_json=True, stdout_stream=buf)
        else:
            ns = argparse.Namespace(repo=args_dict.get("repo"))
            code = handle_claims_list(ns, config, client, force_json=True, stdout_stream=buf)
        result_data = buf.get_json()

    elif name == "bridge_job":
        action = str(args_dict.get("action") or "list").lower()
        if action == "dispatch":
            ns = argparse.Namespace(
                task_id=args_dict.get("task_id") or args_dict.get("job_id"),
                goal=args_dict.get("prompt") or args_dict.get("notes") or args_dict.get("title"),
                capsule=args_dict.get("capsule_id"),
                frozen_globs=args_dict.get("globs") or "prisma/migrations/**",
                verify_cmd=args_dict.get("verify_cmd") or "npm test",
                harness=args_dict.get("harness") or "afk",
                markdown=args_dict.get("markdown"),
            )
            code = handle_jobs_dispatch(ns, config, client, force_json=True, stdout_stream=buf)
        elif action == "capsule":
            ns = argparse.Namespace(
                task_id=args_dict.get("task_id") or args_dict.get("job_id"),
                compile_all=bool(args_dict.get("compile_all", False)),
                frozen_globs=args_dict.get("globs"),
                verify_cmd=args_dict.get("verify_cmd"),
            )
            code = handle_tasks_capsule(ns, config, client, force_json=True, stdout_stream=buf)
        elif action == "claim":
            ns = argparse.Namespace(
                job_id=args_dict.get("job_id") or "",
                globs=args_dict.get("globs") or "src/**",
                ttl=args_dict.get("ttl"),
                branch=args_dict.get("branch"),
                agent=args_dict.get("agent"),
            )
            code = handle_jobs_claim(ns, config, client, force_json=True, stdout_stream=buf)
        elif action == "complete":
            ns = argparse.Namespace(
                job_id=args_dict.get("job_id"),
                pr=args_dict.get("pr_url"),
                commit=args_dict.get("commit_sha"),
                files=args_dict.get("files"),
                notes=args_dict.get("notes"),
                failed=bool(args_dict.get("failed", False)),
            )
            code = handle_jobs_complete(ns, config, client, force_json=True, stdout_stream=buf)
        else:
            ns = argparse.Namespace(
                status=args_dict.get("status"),
                task=args_dict.get("task_id"),
            )
            code = handle_jobs_list(ns, config, client, force_json=True, stdout_stream=buf)
        result_data = buf.get_json()

    elif name == "bridge_fact":
        ns = argparse.Namespace(
            task_id=args_dict.get("task_id") or get_branch_state(config).get("taskId") or "workspace",
            statement=args_dict.get("statement") or "",
            kind=args_dict.get("kind") or "OUTCOME",
            confidence=args_dict.get("confidence") or 90,
        )
        code = handle_tasks_verify(ns, config, client, force_json=True, stdout_stream=buf)
        result_data = buf.get_json()

    else:
        raise ValueError(f"Unknown tool: {name}")

    result_data["exitCode"] = code
    is_error = code not in (0, 4)
    return {
        "content": [
            {
                "type": "text",
                "text": json.dumps(result_data, indent=2, default=str),
            }
        ],
        "isError": is_error,
    }


def handle_jsonrpc_message(
    message: Mapping[str, Any],
    *,
    config: BridgeConfig | None = None,
    client: BridgeClient | None = None,
    cwd: Path | None = None,
) -> dict[str, Any] | None:
    """Process a single JSON-RPC 2.0 request/notification dict and return the response dict (or None for notifications)."""
    cfg = config or resolve_config(cwd=cwd)
    runtime = detect_runtime_context(cwd=cfg.workspace_root)
    cli = client or BridgeClient(cfg, agent_id=runtime["agentId"])

    method = str(message.get("method") or "")
    msg_id = message.get("id")
    params = message.get("params") if isinstance(message.get("params"), Mapping) else {}

    # Notifications (no id field) do not return a JSON-RPC response
    if "id" not in message:
        return None

    try:
        if method == "initialize":
            result = {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {
                    "resources": {"subscribe": False, "listChanged": False},
                    "prompts": {"listChanged": False},
                    "tools": {"listChanged": False},
                },
                "serverInfo": {
                    "name": "bridge-mcp",
                    "version": __version__,
                },
            }
        elif method == "ping":
            result = {}
        elif method == "resources/list":
            result = {"resources": MCP_RESOURCES}
        elif method == "resources/read":
            uri = str(params.get("uri") or "")
            result = read_mcp_resource(uri, cfg, cli)
        elif method == "prompts/list":
            result = {"prompts": MCP_PROMPTS}
        elif method == "prompts/get":
            name = str(params.get("name") or "")
            prompt_args = params.get("arguments") if isinstance(params.get("arguments"), Mapping) else {}
            result = get_mcp_prompt(name, prompt_args, cfg, cli)
        elif method == "tools/list":
            result = {"tools": MCP_TOOLS}
        elif method == "tools/call":
            tool_name = str(params.get("name") or "")
            tool_args = params.get("arguments") if isinstance(params.get("arguments"), Mapping) else {}
            result = call_mcp_tool_handler(tool_name, tool_args, cfg, cli)
        else:
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "error": {
                    "code": -32601,
                    "message": f"Method not found: {method}",
                },
            }

        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": result,
        }
    except Exception as exc:
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "error": {
                "code": -32602,
                "message": str(exc),
            },
        }


def run_stdio_server(
    *,
    stdin_stream: Any = None,
    stdout_stream: Any = None,
    config: BridgeConfig | None = None,
    client: BridgeClient | None = None,
) -> int:
    """Run the JSON-RPC 2.0 MCP server over newline-delimited stdio."""
    in_stream = stdin_stream if stdin_stream is not None else sys.stdin
    out_stream = stdout_stream if stdout_stream is not None else sys.stdout

    for raw_line in in_stream:
        line = str(raw_line).strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except Exception:
            err_resp = {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32700, "message": "Parse error"},
            }
            out_stream.write(json.dumps(err_resp) + "\n")
            out_stream.flush()
            continue

        if isinstance(msg, dict):
            resp = handle_jsonrpc_message(msg, config=config, client=client)
            if resp is not None:
                out_stream.write(json.dumps(resp) + "\n")
                out_stream.flush()
    return EXIT_OK


def main() -> None:
    sys.exit(run_stdio_server())


if __name__ == "__main__":
    main()
