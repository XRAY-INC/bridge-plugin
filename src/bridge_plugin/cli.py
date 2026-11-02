"""Ground-up argparse dispatcher for `bridge` and `brg` with TTY vs. non-TTY JSON auto-switching."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

from . import __version__
from .client import BridgeClient
from .commands.auth import (
    handle_auth_login,
    handle_auth_logout,
    handle_auth_whoami,
    handle_init,
    handle_setup,
    handle_status,
)
from .commands.concurrency import (
    handle_claim,
    handle_claims_list,
    handle_release,
    handle_worktree,
)
from .commands.decide import (
    handle_decide_check,
    handle_decide_list,
    handle_decide_propose,
    handle_decide_resolve,
    handle_decide_score,
    handle_decide_show,
)
from .commands.memory import (
    handle_handoff,
    handle_scratchpad_read,
    handle_scratchpad_write,
)
from .commands.seam import (
    handle_seam_edit,
    handle_seam_flush,
    handle_seam_list,
    handle_seam_log,
    handle_seam_retract,
)
from .commands.tasks import (
    handle_jobs_claim,
    handle_jobs_complete,
    handle_jobs_list,
    handle_tasks_list,
    handle_tasks_start,
    handle_tasks_verify,
)
from .config import resolve_config
from .mcp_server import run_stdio_server
from .runtime import (
    EXIT_ERROR,
    detect_runtime_context,
    emit_output,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bridge",
        description="Bridge CLI (bridge / brg) — Seams, 1-Click Decisions, Scope Claims, Task Branching & MCP Server.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"bridge-plugin {__version__}",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        default=False,
        help="Force deterministic JSON output on stdout.",
    )
    parser.add_argument("--org", help="Override Bridge organizationId.")
    parser.add_argument("--url", help="Override Bridge API URL.")
    parser.add_argument("--repo", help="Override repository slug (owner/name).")
    parser.add_argument("--api-key", dest="global_api_key", help="Override Bridge API key.")

    subparsers = parser.add_subparsers(dest="command")

    # 1. Onboarding & Setup: auth, init (aliases: connect, link), status, setup
    auth_parser = subparsers.add_parser("auth", help="Authenticate with Bridge (login, whoami, logout).")
    auth_sub = auth_parser.add_subparsers(dest="auth_action")

    login_p = auth_sub.add_parser("login", help="Save Bridge API key to ~/.config/bridge/credentials.json.")
    login_p.add_argument("--api-key", help="Bridge API key (xrkey_v1...) or '-' for stdin.")
    login_p.add_argument("--url", help="Bridge API base URL.")
    login_p.add_argument("--org", help="Default organization ID.")
    login_p.add_argument("--email", help="Developer email address.")
    login_p.add_argument("--json", action="store_true", default=False)

    whoami_p = auth_sub.add_parser("whoami", help="Show active Bridge authentication.")
    whoami_p.add_argument("--verify", action="store_true", help="Verify key against Bridge API.")
    whoami_p.add_argument("--json", action="store_true", default=False)

    logout_p = auth_sub.add_parser("logout", help="Remove saved Bridge credentials.")
    logout_p.add_argument("--json", action="store_true", default=False)

    for init_cmd in ("init", "connect", "link"):
        init_p = subparsers.add_parser(init_cmd, help="Bind repository to Bridge by writing .bridge.json.")
        init_p.add_argument("--org", help="Bridge organization ID.")
        init_p.add_argument("--repo", help="Repository slug (owner/name).")
        init_p.add_argument("--url", help="Bridge API URL.")
        init_p.add_argument("--json", action="store_true", default=False)

    status_p = subparsers.add_parser("status", help="Show Bridge workspace status, bound task, claims, and spool.")
    status_p.add_argument("--json", action="store_true", default=False)

    setup_p = subparsers.add_parser(
        "setup",
        help="Configure Bridge MCP server, SKILL.md, or git hooks (claude, cursor, antigravity, codex, git-hooks).",
    )
    setup_p.add_argument(
        "target",
        choices=["claude", "cursor", "antigravity", "codex", "git-hooks"],
        help="Target harness or git-hooks.",
    )
    setup_p.add_argument("--json", action="store_true", default=False)

    # 2. Seams: win, decision, friction, handoff, list, edit, retract (rm/delete), flush
    seam_parser = subparsers.add_parser("seam", help="Log, list, edit, retract, or flush interstitial seams.")
    seam_sub = seam_parser.add_subparsers(dest="seam_action")

    for tag_name in ("win", "decision", "friction", "handoff"):
        tag_p = seam_sub.add_parser(tag_name, help=f"Log a {tag_name.upper()} seam.")
        tag_p.add_argument("summary", help="Seam summary text (or '-' to read from stdin).")
        tag_p.add_argument("--task", help="Task ID (defaults to current branch binding).")
        tag_p.add_argument("--job", help="Agent Job ID.")
        tag_p.add_argument("--branch", help="Git branch override.")
        tag_p.add_argument("--commit", help="Commit SHA override.")
        tag_p.add_argument("--repo", help="Repository slug override.")
        tag_p.add_argument("--agent", help="Agent ID override.")
        tag_p.add_argument("--confidence", type=int, help="Confidence (1-100).")
        tag_p.add_argument("--json", action="store_true", default=False)

    seam_list_p = seam_sub.add_parser("list", help="List recent seams.")
    seam_list_p.add_argument("--tag", help="Filter by tag (WIN, DECISION, FRICTION, HANDOFF).")
    seam_list_p.add_argument("--repo", help="Filter by repository slug.")
    seam_list_p.add_argument("--task", help="Filter by task ID.")
    seam_list_p.add_argument("--limit", type=int, default=25, help="Max records.")
    seam_list_p.add_argument("--json", action="store_true", default=False)

    seam_edit_p = seam_sub.add_parser("edit", help="Edit an existing seam in place.")
    seam_edit_p.add_argument("seam_id", help="Seam ID or 'last'.")
    seam_edit_p.add_argument("summary", help="Updated summary (or '-' for stdin).")
    seam_edit_p.add_argument("--tag", help="Optional updated tag.")
    seam_edit_p.add_argument("--json", action="store_true", default=False)

    for retract_cmd in ("retract", "rm", "delete"):
        seam_retract_p = seam_sub.add_parser(retract_cmd, help="Soft-retract a seam by ID or 'last'.")
        seam_retract_p.add_argument("seam_id", help="Seam ID or 'last'.")
        seam_retract_p.add_argument("--json", action="store_true", default=False)

    seam_flush_p = seam_sub.add_parser("flush", help="Idempotently replay queued offline seams from .bridge/spool.jsonl.")
    seam_flush_p.add_argument("--json", action="store_true", default=False)

    # 3. Decide Governance: list, show, propose, score, resolve, check
    decide_parser = subparsers.add_parser("decide", help="1-Click Decisions governance (list, show, propose, score, resolve, check).")
    decide_sub = decide_parser.add_subparsers(dest="decide_action")

    dec_list_p = decide_sub.add_parser("list", help="List workspace decisions.")
    dec_list_p.add_argument("--status", help="Filter by status (OPEN, DECIDED).")
    dec_list_p.add_argument("--urgency", help="Filter by urgency (CRITICAL, HIGH, STANDARD, LOW).")
    dec_list_p.add_argument("--task", help="Filter by blockedTaskId.")
    dec_list_p.add_argument("--limit", type=int, default=25)
    dec_list_p.add_argument("--json", action="store_true", default=False)

    dec_show_p = decide_sub.add_parser("show", help="Show details and rankings for a decision.")
    dec_show_p.add_argument("decision_id", help="Decision ID.")
    dec_show_p.add_argument("--json", action="store_true", default=False)

    dec_prop_p = decide_sub.add_parser("propose", help="Escalate an architectural trade-off as a 1-Click Decision Card.")
    dec_prop_p.add_argument("--title", required=True, help="Decision title.")
    dec_prop_p.add_argument("--context", required=True, help="Trade-off context summary.")
    dec_prop_p.add_argument("--urgency", default="STANDARD", help="Urgency (CRITICAL, HIGH, STANDARD, LOW).")
    dec_prop_p.add_argument("--options", help="Comma-separated option labels or JSON array.")
    dec_prop_p.add_argument("--criteria", help="Comma-separated 'Name:Weight' pairs or JSON array.")
    dec_prop_p.add_argument("--task", help="Blocked task ID (defaults to current branch task).")
    dec_prop_p.add_argument("--job", help="Source agent job ID.")
    dec_prop_p.add_argument("--json", action="store_true", default=False)

    dec_score_p = decide_sub.add_parser("score", help="Score an option against a criterion (1-10).")
    dec_score_p.add_argument("decision_id", help="Decision ID.")
    dec_score_p.add_argument("--option", required=True, help="Option ID or 0-based index.")
    dec_score_p.add_argument("--criterion", required=True, help="Criterion ID or 0-based index.")
    dec_score_p.add_argument("--score", type=int, required=True, help="Score from 1 to 10.")
    dec_score_p.add_argument("--rationale", help="Score rationale.")
    dec_score_p.add_argument("--weight", type=int, help="Optional criterion weight update (1-10).")
    dec_score_p.add_argument("--json", action="store_true", default=False)

    dec_res_p = decide_sub.add_parser("resolve", help="Endorse an option and mark decision DECIDED.")
    dec_res_p.add_argument("decision_id", help="Decision ID.")
    dec_res_p.add_argument("--option", required=True, help="Chosen Option ID.")
    dec_res_p.add_argument("--rationale", help="Decision rationale.")
    dec_res_p.add_argument("--json", action="store_true", default=False)

    dec_chk_p = decide_sub.add_parser("check", help="Check if active task has an open blocking decision (exits 2 if blocked).")
    dec_chk_p.add_argument("--task", help="Task ID override.")
    dec_chk_p.add_argument("--json", action="store_true", default=False)

    # 4. Tasks & Agent Jobs
    tasks_parser = subparsers.add_parser("tasks", help="List, start (issue-driven branching), or verify tasks.")
    tasks_sub = tasks_parser.add_subparsers(dest="tasks_action")

    t_list_p = tasks_sub.add_parser("list", help="List workspace tasks.")
    t_list_p.add_argument("--status", help="Filter by status (TODO, IN_PROGRESS, BLOCKED_ON_DECISION, DONE).")
    t_list_p.add_argument("--json", action="store_true", default=False)

    t_start_p = tasks_sub.add_parser("start", help="Checkout feat/<taskId>-<slug> and bind branch -> taskId in .bridge/state.json.")
    t_start_p.add_argument("task_id", help="Task ID.")
    t_start_p.add_argument("--slug", help="Branch slug suffix.")
    t_start_p.add_argument("--branch", help="Explicit branch name.")
    t_start_p.add_argument("--job", help="Associated agent job ID.")
    t_start_p.add_argument("--worktree", action="store_true", help="Create isolated git worktree.")
    t_start_p.add_argument("--no-checkout", action="store_true", help="Bind state without git checkout.")
    t_start_p.add_argument("--json", action="store_true", default=False)

    t_verify_p = tasks_sub.add_parser("verify", help="Ingest bounded <=280 char verification fact for P16 two-sided reconciliation.")
    t_verify_p.add_argument("task_id", help="Task ID.")
    t_verify_p.add_argument("statement", help="Verification statement (or '-' for stdin).")
    t_verify_p.add_argument("--kind", default="OUTCOME", help="Fact kind (OUTCOME, DECISION, COMMITMENT, BLOCKER, ACCOMPLISHMENT).")
    t_verify_p.add_argument("--confidence", type=int, default=90, help="Confidence (1-100).")
    t_verify_p.add_argument("--json", action="store_true", default=False)

    jobs_parser = subparsers.add_parser("jobs", help="List, claim, or complete dispatched agent jobs.")
    jobs_sub = jobs_parser.add_subparsers(dest="jobs_action")

    j_list_p = jobs_sub.add_parser("list", help="List agent jobs.")
    j_list_p.add_argument("--status", help="Filter by status (QUEUED, CLAIMED, COMPLETED, BLOCKED_ON_DECISION).")
    j_list_p.add_argument("--task", help="Filter by task ID.")
    j_list_p.add_argument("--json", action="store_true", default=False)

    j_claim_p = jobs_sub.add_parser("claim", help="Atomically claim a dispatched agent job (exits 2 on 409 conflict).")
    j_claim_p.add_argument("job_id", help="Job ID.")
    j_claim_p.add_argument("--globs", default="src/**", help="Comma-separated scope globs.")
    j_claim_p.add_argument("--ttl", type=int, help="Lease TTL in minutes.")
    j_claim_p.add_argument("--branch", help="Worktree branch name.")
    j_claim_p.add_argument("--agent", help="Agent ID override.")
    j_claim_p.add_argument("--json", action="store_true", default=False)

    j_comp_p = jobs_sub.add_parser("complete", help="Mark a claimed agent job completed.")
    j_comp_p.add_argument("job_id", nargs="?", help="Job ID (defaults to bound branch jobId).")
    j_comp_p.add_argument("--pr", help="Pull request URL.")
    j_comp_p.add_argument("--commit", help="Commit SHA.")
    j_comp_p.add_argument("--files", help="Comma-separated modified files.")
    j_comp_p.add_argument("--notes", help="Completion notes.")
    j_comp_p.add_argument("--failed", action="store_true", help="Mark verification as failed.")
    j_comp_p.add_argument("--json", action="store_true", default=False)

    # 5. Concurrency: claim, release, claims (leases), worktree
    claim_p = subparsers.add_parser("claim", help="Claim file/directory globs across worktrees (exits 2 on overlap conflict).")
    claim_p.add_argument("globs", nargs="+", help="One or more glob patterns (e.g. 'src/auth/*').")
    claim_p.add_argument("--ttl", default="30m", help="Lease duration (e.g. 30m, 1800s).")
    claim_p.add_argument("--reason", help="Reason for claiming scope.")
    claim_p.add_argument("--branch", help="Branch override.")
    claim_p.add_argument("--repo", help="Repository override.")
    claim_p.add_argument("--agent", help="Agent ID override.")
    claim_p.add_argument("--json", action="store_true", default=False)

    release_p = subparsers.add_parser("release", help="Release active scope claims.")
    release_p.add_argument("--lease-id", help="Specific lease ID to release.")
    release_p.add_argument("--all", action="store_true", help="Release all of this agent's claims.")
    release_p.add_argument("--everyone", action="store_true", help="Release every agent's claims in this repository.")
    release_p.add_argument("--repo", help="Repository override.")
    release_p.add_argument("--agent", help="Agent ID override.")
    release_p.add_argument("--json", action="store_true", default=False)

    for claims_cmd in ("claims", "leases"):
        claims_p = subparsers.add_parser(claims_cmd, help="List active scope claims.")
        claims_p.add_argument("--repo", help="Filter by repository.")
        claims_p.add_argument("--json", action="store_true", default=False)

    wt_parser = subparsers.add_parser("worktree", help="Manage isolated Git worktrees (list, create, rm).")
    wt_sub = wt_parser.add_subparsers(dest="worktree_action")
    wt_list = wt_sub.add_parser("list", help="List Git worktrees.")
    wt_list.add_argument("--json", action="store_true", default=False)
    wt_create = wt_sub.add_parser("create", help="Create isolated worktree in .worktrees/<slug>.")
    wt_create.add_argument("slug", help="Worktree slug.")
    wt_create.add_argument("--branch", help="Branch name.")
    wt_create.add_argument("--json", action="store_true", default=False)
    for rm_cmd in ("rm", "remove"):
        wt_rm = wt_sub.add_parser(rm_cmd, help="Remove isolated worktree.")
        wt_rm.add_argument("slug", help="Worktree slug.")
        wt_rm.add_argument("--json", action="store_true", default=False)

    # 6. Working Memory: scratchpad (read/write), handoff
    sp_parser = subparsers.add_parser("scratchpad", help="Read or write working-memory scratchpad notes.")
    sp_sub = sp_parser.add_subparsers(dest="scratchpad_action")

    sp_read = sp_sub.add_parser("read", help="Read working-memory scratchpad notes.")
    sp_read.add_argument("--topic", help="Filter by topic.")
    sp_read.add_argument("--repo", help="Filter by repository.")
    sp_read.add_argument("--limit", type=int, default=20)
    sp_read.add_argument("--json", action="store_true", default=False)

    sp_write = sp_sub.add_parser("write", help="Upsert a working-memory scratchpad note.")
    sp_write.add_argument("topic", help="Note topic.")
    sp_write.add_argument("body", help="Markdown body (or '-' for stdin).")
    sp_write.add_argument("--repo", help="Repository override.")
    sp_write.add_argument("--branch", help="Branch override.")
    sp_write.add_argument("--json", action="store_true", default=False)

    handoff_p = subparsers.add_parser("handoff", help="Log a turn-closure HANDOFF seam and update scratchpad.")
    handoff_p.add_argument("summary", help="Handoff summary (or '-' for stdin).")
    handoff_p.add_argument("--topic", help="Optional scratchpad topic.")
    handoff_p.add_argument("--task", help="Optional task ID.")
    handoff_p.add_argument("--branch", help="Optional branch override.")
    handoff_p.add_argument("--repo", help="Optional repository override.")
    handoff_p.add_argument("--json", action="store_true", default=False)

    # 7. MCP Server
    mcp_p = subparsers.add_parser("mcp", help="Run the stdio JSON-RPC 2.0 MCP server.")
    mcp_p.add_argument("--json", action="store_true", default=False)

    return parser


def run_cli(
    argv: Sequence[str] | None = None,
    *,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
    stdin_stream: Any = None,
    stdout_stream: Any = None,
    transport: Any = None,
) -> int:
    """Execute a Bridge CLI command and return its semantic exit code (0-4)."""
    parser = build_parser()
    args_list = list(argv) if argv is not None else sys.argv[1:]
    force_json = "--json" in args_list

    # Strip --json from subparser collisions while keeping it on parsed namespace
    cleaned_args = [a for a in args_list if a != "--json"]
    if not cleaned_args:
        parser.print_help(file=stdout_stream or sys.stdout)
        return EXIT_ERROR

    try:
        args = parser.parse_args(cleaned_args)
    except SystemExit as exc:
        return int(exc.code) if isinstance(exc.code, int) else EXIT_ERROR

    args.json = force_json

    config = resolve_config(
        cwd=cwd,
        env=env,
        explicit_org=getattr(args, "org", None),
        explicit_url=getattr(args, "url", None),
        explicit_repo=getattr(args, "repo", None),
        explicit_api_key=getattr(args, "global_api_key", None),
    )
    runtime = detect_runtime_context(cwd=config.workspace_root, env=env)
    client = BridgeClient(config, agent_id=runtime["agentId"], transport=transport, env=env)

    cmd = getattr(args, "command", None)

    if cmd == "auth":
        sub = getattr(args, "auth_action", "whoami")
        if sub == "login":
            return handle_auth_login(
                args,
                config,
                client,
                force_json=force_json,
                stdin_stream=stdin_stream,
                stdout_stream=stdout_stream,
            )
        if sub == "logout":
            return handle_auth_logout(
                args,
                config,
                force_json=force_json,
                stdout_stream=stdout_stream,
            )
        return handle_auth_whoami(
            args,
            config,
            client,
            force_json=force_json,
            stdout_stream=stdout_stream,
        )

    if cmd in ("init", "connect", "link"):
        return handle_init(args, config, force_json=force_json, stdout_stream=stdout_stream)

    if cmd == "status":
        return handle_status(args, config, client, force_json=force_json, stdout_stream=stdout_stream)

    if cmd == "setup":
        return handle_setup(args, config, force_json=force_json, stdout_stream=stdout_stream)

    if cmd == "seam":
        sub = getattr(args, "seam_action", "list")
        if sub in ("win", "decision", "friction", "handoff"):
            args.tag = sub.upper()
            return handle_seam_log(
                args,
                config,
                client,
                force_json=force_json,
                stdin_stream=stdin_stream,
                stdout_stream=stdout_stream,
            )
        if sub == "list":
            return handle_seam_list(args, config, client, force_json=force_json, stdout_stream=stdout_stream)
        if sub == "edit":
            return handle_seam_edit(
                args,
                config,
                client,
                force_json=force_json,
                stdin_stream=stdin_stream,
                stdout_stream=stdout_stream,
            )
        if sub in ("retract", "rm", "delete"):
            return handle_seam_retract(args, config, client, force_json=force_json, stdout_stream=stdout_stream)
        if sub == "flush":
            return handle_seam_flush(args, config, client, force_json=force_json, stdout_stream=stdout_stream)

    if cmd == "decide":
        sub = getattr(args, "decide_action", "list")
        if sub == "list":
            return handle_decide_list(args, config, client, force_json=force_json, stdout_stream=stdout_stream)
        if sub == "show":
            return handle_decide_show(args, config, client, force_json=force_json, stdout_stream=stdout_stream)
        if sub == "propose":
            return handle_decide_propose(args, config, client, force_json=force_json, stdout_stream=stdout_stream)
        if sub == "score":
            return handle_decide_score(args, config, client, force_json=force_json, stdout_stream=stdout_stream)
        if sub == "resolve":
            return handle_decide_resolve(args, config, client, force_json=force_json, stdout_stream=stdout_stream)
        if sub == "check":
            return handle_decide_check(args, config, client, force_json=force_json, stdout_stream=stdout_stream)

    if cmd == "tasks":
        sub = getattr(args, "tasks_action", "list")
        if sub == "list":
            return handle_tasks_list(args, config, client, force_json=force_json, stdout_stream=stdout_stream)
        if sub == "start":
            return handle_tasks_start(args, config, client, force_json=force_json, stdout_stream=stdout_stream)
        if sub == "verify":
            return handle_tasks_verify(
                args,
                config,
                client,
                force_json=force_json,
                stdin_stream=stdin_stream,
                stdout_stream=stdout_stream,
            )

    if cmd == "jobs":
        sub = getattr(args, "jobs_action", "list")
        if sub == "list":
            return handle_jobs_list(args, config, client, force_json=force_json, stdout_stream=stdout_stream)
        if sub == "claim":
            return handle_jobs_claim(args, config, client, force_json=force_json, stdout_stream=stdout_stream)
        if sub == "complete":
            return handle_jobs_complete(args, config, client, force_json=force_json, stdout_stream=stdout_stream)

    if cmd == "claim":
        return handle_claim(args, config, client, force_json=force_json, stdout_stream=stdout_stream)

    if cmd == "release":
        return handle_release(args, config, client, force_json=force_json, stdout_stream=stdout_stream)

    if cmd in ("claims", "leases"):
        return handle_claims_list(args, config, client, force_json=force_json, stdout_stream=stdout_stream)

    if cmd == "worktree":
        return handle_worktree(args, config, force_json=force_json, stdout_stream=stdout_stream)

    if cmd == "scratchpad":
        sub = getattr(args, "scratchpad_action", "read")
        if sub == "write":
            return handle_scratchpad_write(
                args,
                config,
                client,
                force_json=force_json,
                stdin_stream=stdin_stream,
                stdout_stream=stdout_stream,
            )
        return handle_scratchpad_read(args, config, client, force_json=force_json, stdout_stream=stdout_stream)

    if cmd == "handoff":
        return handle_handoff(
            args,
            config,
            client,
            force_json=force_json,
            stdin_stream=stdin_stream,
            stdout_stream=stdout_stream,
        )

    if cmd == "mcp":
        return run_stdio_server(stdin_stream=stdin_stream, stdout_stream=stdout_stream, config=config, client=client)

    emit_output(
        {"ok": False, "error": "UNKNOWN_COMMAND", "command": cmd},
        f"Unknown command: {cmd}",
        force_json=force_json,
        stdout_stream=stdout_stream,
    )
    return EXIT_ERROR


def main() -> None:
    sys.exit(run_cli())


if __name__ == "__main__":
    main()
