"""1-Click Decisions governance commands: list, show, propose, score, resolve, and check."""

from __future__ import annotations

import json
from typing import Any

from ..client import BridgeClient
from ..config import BridgeConfig, get_branch_state
from ..runtime import (
    EXIT_CONCURRENCY_BLOCK,
    EXIT_ERROR,
    EXIT_OK,
    compute_idempotency_key,
    detect_git_branch,
    detect_git_commit_sha,
    emit_output,
    format_table,
)


def _parse_options_input(raw_options: str | None) -> list[dict[str, Any]]:
    if not raw_options:
        return [
            {"label": "Option A (Recommended)", "summary": "Primary recommended path", "isRecommended": True},
            {"label": "Option B (Alternative)", "summary": "Alternative trade-off path"},
        ]
    stripped = raw_options.strip()
    if stripped.startswith("["):
        try:
            parsed = json.loads(stripped)
            if isinstance(parsed, list):
                return [
                    item if isinstance(item, dict) else {"label": str(item), "summary": str(item)}
                    for item in parsed
                ]
        except Exception:
            pass
    parts = [p.strip() for p in stripped.split(",") if p.strip()]
    return [
        {
            "label": part,
            "summary": part,
            "isRecommended": idx == 0,
        }
        for idx, part in enumerate(parts)
    ]


def _parse_criteria_input(raw_criteria: str | None) -> list[dict[str, Any]]:
    if not raw_criteria:
        return []
    stripped = raw_criteria.strip()
    if stripped.startswith("["):
        try:
            parsed = json.loads(stripped)
            if isinstance(parsed, list):
                return [c for c in parsed if isinstance(c, dict)]
        except Exception:
            pass
    criteria: list[dict[str, Any]] = []
    for item in stripped.split(","):
        part = item.strip()
        if not part:
            continue
        if ":" in part:
            name, weight_str = part.split(":", 1)
            try:
                weight = max(1, min(10, int(weight_str.strip())))
            except ValueError:
                weight = 5
            criteria.append({"name": name.strip(), "weight": weight})
        else:
            criteria.append({"name": part, "weight": 5})
    return criteria


def _parse_recipients_input(raw_to: str | list[Any] | None) -> list[str]:
    if not raw_to:
        return []
    if isinstance(raw_to, list):
        return [str(item).strip() for item in raw_to if str(item).strip()]
    stripped = str(raw_to).strip()
    if not stripped:
        return []
    if stripped.startswith("["):
        try:
            parsed = json.loads(stripped)
            if isinstance(parsed, list):
                return [str(item).strip() for item in parsed if str(item).strip()]
        except Exception:
            pass
    return [part.strip() for part in stripped.replace(";", ",").split(",") if part.strip()]


def _parse_scores_input(raw_scores: str | list[Any] | None) -> list[dict[str, Any]]:
    if not raw_scores:
        return []
    if isinstance(raw_scores, list):
        return [item for item in raw_scores if isinstance(item, dict)]
    stripped = str(raw_scores).strip()
    if stripped.startswith("["):
        try:
            parsed = json.loads(stripped)
            if isinstance(parsed, list):
                return [item for item in parsed if isinstance(item, dict)]
        except Exception:
            pass
    scores: list[dict[str, Any]] = []
    for item in stripped.split(","):
        part = item.strip()
        if not part:
            continue
        segments = part.split(":", 3)
        if len(segments) >= 3:
            opt_ref, crit_ref, score_str = segments[0].strip(), segments[1].strip(), segments[2].strip()
            try:
                score_val = max(1, min(10, int(score_str)))
            except ValueError:
                score_val = 5
            entry: dict[str, Any] = {"score": score_val}
            if opt_ref.isdigit():
                entry["optionIndex"] = int(opt_ref)
            else:
                entry["optionId"] = opt_ref
            if crit_ref.isdigit():
                entry["criterionIndex"] = int(crit_ref)
            else:
                entry["criterionId"] = crit_ref
            if len(segments) == 4 and segments[3].strip():
                entry["rationale"] = segments[3].strip()
            scores.append(entry)
    return scores


def _parse_flows_input(raw_flows: str | list[Any] | None) -> list[dict[str, Any]]:
    if not raw_flows:
        return []
    if isinstance(raw_flows, list):
        return [item for item in raw_flows if isinstance(item, dict)]
    stripped = str(raw_flows).strip()
    if stripped.startswith("["):
        try:
            parsed = json.loads(stripped)
            if isinstance(parsed, list):
                return [item for item in parsed if isinstance(item, dict)]
        except Exception:
            pass
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    for idx, item in enumerate(stripped.split("|")):
        part = item.strip()
        if not part:
            continue
        segments = [s.strip() for s in part.split(":")]
        node_id = segments[0] if len(segments) >= 2 else f"n{idx + 1}"
        label = segments[1] if len(segments) >= 2 else segments[0]
        kind = segments[2] if len(segments) >= 3 else ("trigger" if idx == 0 else "impact")
        nodes.append({"id": node_id, "label": label, "kind": kind})
        if idx > 0:
            edges.append({"from": nodes[idx - 1]["id"], "to": node_id})
    return [{"optionIndex": 0, "nodes": nodes, "edges": edges}] if nodes else []


def handle_decide_list(
    args: Any,
    config: BridgeConfig,
    client: BridgeClient,
    *,
    force_json: bool = False,
    stdout_stream: Any = None,
) -> int:
    """List decisions for the workspace."""
    params: dict[str, Any] = {}
    if getattr(args, "status", None):
        params["status"] = str(args.status).upper()
    if getattr(args, "urgency", None):
        params["urgency"] = str(args.urgency).upper()
    if getattr(args, "task", None):
        params["blockedTaskId"] = args.task
    if getattr(args, "limit", None):
        params["limit"] = int(args.limit)

    resp = client.request("GET", "/api/v1/decide", params=params)
    decisions = resp.data.get("decisions", []) if isinstance(resp.data.get("decisions"), list) else []
    rows = []
    for d in decisions:
        if not isinstance(d, dict):
            continue
        rankings = d.get("rankings", [])
        top_opt = rankings[0].get("label", "-") if isinstance(rankings, list) and rankings else "-"
        rows.append(
            [
                d.get("id", ""),
                d.get("status", "OPEN"),
                d.get("urgency", "STANDARD"),
                d.get("blockedTaskId") or "-",
                top_opt,
                (d.get("title") or "")[:48],
            ]
        )
    table = format_table(["ID", "STATUS", "URGENCY", "BLOCKED_TASK", "TOP_OPTION", "TITLE"], rows)
    emit_output(resp.data, table, force_json=force_json, stdout_stream=stdout_stream)
    return resp.exit_code


def handle_decide_show(
    args: Any,
    _config: BridgeConfig,
    client: BridgeClient,
    *,
    force_json: bool = False,
    stdout_stream: Any = None,
) -> int:
    """Show details, options, criteria, and weighted rankings for a single decision."""
    decision_id = str(getattr(args, "decision_id", "")).strip()
    if not decision_id:
        emit_output(
            {"ok": False, "error": "DECISION_ID_REQUIRED"},
            "Error: Decision ID is required.",
            force_json=force_json,
            stdout_stream=stdout_stream,
        )
        return EXIT_ERROR

    resp = client.request("GET", "/api/v1/decide", params={"decisionId": decision_id})
    decisions = resp.data.get("decisions", []) if isinstance(resp.data.get("decisions"), list) else []
    decision = decisions[0] if decisions else resp.data.get("decision")
    if not isinstance(decision, dict):
        emit_output(
            {"ok": False, "error": "DECISION_NOT_FOUND", "decisionId": decision_id},
            f"Decision '{decision_id}' not found.",
            force_json=force_json,
            stdout_stream=stdout_stream,
        )
        return EXIT_ERROR

    payload = {"ok": True, "decision": decision}
    options = decision.get("options", []) if isinstance(decision.get("options"), list) else []
    rankings = decision.get("rankings", []) if isinstance(decision.get("rankings"), list) else []
    human_lines = [
        f"Decision {decision.get('id')} [{decision.get('status', 'OPEN')} · {decision.get('urgency', 'STANDARD')}]",
        f"  Title        : {decision.get('title')}",
        f"  Context      : {decision.get('contextSummary')}",
        f"  Blocked Task : {decision.get('blockedTaskId') or '(none)'}",
        "  Options      :",
        *[f"    - [{opt.get('id')}] {opt.get('label')}: {opt.get('description') or opt.get('summary') or ''}" for opt in options if isinstance(opt, dict)],
    ]
    if rankings:
        human_lines.append("  Weighted Rankings:")
        for r in rankings:
            if isinstance(r, dict):
                human_lines.append(
                    f"    #{r.get('rank', '?')} {r.get('label', r.get('optionId'))} — weightedScore={r.get('weightedScore', 0)}"
                )

    emit_output(payload, human_lines, force_json=force_json, stdout_stream=stdout_stream)
    return EXIT_OK


def handle_decide_propose(
    args: Any,
    config: BridgeConfig,
    client: BridgeClient,
    *,
    force_json: bool = False,
    stdout_stream: Any = None,
) -> int:
    """Escalate a trade-off to Bridge 1-Click Decisions (/api/v1/decide/escalate)."""
    title = str(getattr(args, "title", "") or "").strip()
    context_summary = str(getattr(args, "context", "") or "").strip()
    if not title or not context_summary:
        emit_output(
            {
                "ok": False,
                "error": "TITLE_AND_CONTEXT_REQUIRED",
                "message": "Both --title and --context are required to propose a decision.",
            },
            "Error: Provide both --title and --context.",
            force_json=force_json,
            stdout_stream=stdout_stream,
        )
        return EXIT_ERROR

    branch = detect_git_branch(cwd=config.workspace_root, env=config.env)
    commit_sha = detect_git_commit_sha(cwd=config.workspace_root, env=config.env)
    branch_state = get_branch_state(config, branch=branch)

    blocked_task_id = getattr(args, "task", None) or branch_state.get("taskId")
    source_job_id = getattr(args, "job", None) or branch_state.get("jobId")
    urgency = str(getattr(args, "urgency", "STANDARD") or "STANDARD").strip().upper()
    options = _parse_options_input(getattr(args, "options", None))
    criteria = _parse_criteria_input(getattr(args, "criteria", None))
    recipients = _parse_recipients_input(getattr(args, "to", None) or getattr(args, "recipients", None))
    scores = _parse_scores_input(getattr(args, "scores", None))
    flows = _parse_flows_input(getattr(args, "flow", None) or getattr(args, "flows", None))
    raw_mode = "ADVISORY" if getattr(args, "advisory", False) else str(getattr(args, "mode", "blocking") or "blocking").strip().upper()
    mode = "ADVISORY" if raw_mode == "ADVISORY" else "BLOCKING"

    idempotency_key = compute_idempotency_key(
        org_id=config.organization_id or "",
        repo=config.repository or "",
        branch=branch,
        commit_sha=commit_sha,
        tag="DECIDE_PROPOSE",
        summary=f"{title}:{context_summary}:{mode}:{','.join(recipients)}",
    )

    body: dict[str, Any] = {
        "title": title,
        "contextSummary": context_summary,
        "urgency": urgency,
        "mode": mode,
        "blockedTaskId": blocked_task_id,
        "sourceAgentJobId": source_job_id,
        "options": options,
    }
    if criteria:
        body["criteria"] = criteria
    if recipients:
        body["to"] = recipients
        body["recipients"] = recipients
    if scores:
        body["scores"] = scores
    if flows:
        body["flows"] = flows
    if config.organization_id:
        body["organizationId"] = config.organization_id
    if config.repository:
        body["repository"] = config.repository
    if config.project_name:
        body["project"] = config.project_name
        body["projectName"] = config.project_name
    if config.user_email:
        body["proposerEmail"] = config.user_email
        body["userEmail"] = config.user_email

    resp = client.request(
        "POST",
        "/api/v1/decide/escalate",
        json_body=body,
        idempotency_key=idempotency_key,
    )
    decision_obj = resp.data.get("decision") if isinstance(resp.data.get("decision"), dict) else {}
    dec_id = decision_obj.get("id") or resp.data.get("decisionId") or "created"
    distribution = resp.data.get("distribution") if isinstance(resp.data.get("distribution"), dict) else {}
    guest_links = resp.data.get("guestLinks") if isinstance(resp.data.get("guestLinks"), list) else []

    if resp.ok:
        mode_badge = "ADVISORY · Default-Unless-Vetoed" if mode == "ADVISORY" else "BLOCKING"
        human_lines = [
            f"Proposed Decision Card [{dec_id}] ({mode_badge}): {title}"
            + (f" (task {blocked_task_id})" if blocked_task_id else "")
        ]
        dist_recs = (
            distribution.get("deliveries")
            if isinstance(distribution.get("deliveries"), list)
            else distribution.get("recipients")
            if isinstance(distribution.get("recipients"), list)
            else []
        )
        if dist_recs:
            rec_labels = [
                f"{r.get('displayName') or r.get('name') or r.get('handle') or r.get('email')} <{r.get('email')}>"
                for r in dist_recs
                if isinstance(r, dict)
            ]
            human_lines.append(f"  Distributed to : {', '.join(rec_labels)}")
        elif recipients:
            human_lines.append(f"  Recipients     : {', '.join(recipients)}")
        for link in guest_links:
            if isinstance(link, dict):
                vote_url = link.get("shareUrl") or link.get("voteUrl")
                link_email = link.get("guestEmail") or link.get("email")
                if vote_url:
                    human_lines.append(f"  1-Click Link   : {link_email} -> {vote_url}")
        human: str | list[str] = human_lines if len(human_lines) > 1 else human_lines[0]
    else:
        human = f"Error proposing decision: {resp.error}"

    emit_output(
        {**resp.data, "idempotencyKey": idempotency_key},
        human,
        force_json=force_json,
        stdout_stream=stdout_stream,
    )
    return resp.exit_code


def handle_decide_score(
    args: Any,
    config: BridgeConfig,
    client: BridgeClient,
    *,
    force_json: bool = False,
    stdout_stream: Any = None,
) -> int:
    """Submit a criterion matrix score (1-10) for an option on a decision."""
    decision_id = str(getattr(args, "decision_id", "")).strip()
    raw_opt = str(getattr(args, "option", "")).strip()
    raw_crit = str(getattr(args, "criterion", "")).strip()
    score_val = int(getattr(args, "score", 0))

    score_entry: dict[str, Any] = {"score": score_val}
    if raw_opt.isdigit():
        score_entry["optionIndex"] = int(raw_opt)
    else:
        score_entry["optionId"] = raw_opt

    if raw_crit.isdigit():
        score_entry["criterionIndex"] = int(raw_crit)
    else:
        score_entry["criterionId"] = raw_crit

    if getattr(args, "rationale", None):
        score_entry["rationale"] = str(args.rationale).strip()
    if getattr(args, "weight", None) is not None:
        score_entry["weight"] = int(args.weight)

    body: dict[str, Any] = {
        "scores": [score_entry],
    }
    if config.organization_id:
        body["organizationId"] = config.organization_id

    resp = client.request("POST", f"/api/v1/decide/{decision_id}/score", json_body=body)
    human = (
        f"Recorded score {score_val}/10 on decision {decision_id}."
        if resp.ok
        else f"Error scoring decision {decision_id}: {resp.error}"
    )
    emit_output(resp.data, human, force_json=force_json, stdout_stream=stdout_stream)
    return resp.exit_code


def handle_decide_resolve(
    args: Any,
    config: BridgeConfig,
    client: BridgeClient,
    *,
    force_json: bool = False,
    stdout_stream: Any = None,
) -> int:
    """Endorse/resolve an option on a decision and unblock any blocked task."""
    decision_id = str(getattr(args, "decision_id", "")).strip()
    option_id = str(getattr(args, "option", "")).strip()
    rationale = getattr(args, "rationale", None)

    body: dict[str, Any] = {
        "action": "ENDORSE",
        "decisionId": decision_id,
        "chosenOptionId": option_id,
    }
    if rationale:
        body["rationale"] = str(rationale).strip()
    if config.organization_id:
        body["organizationId"] = config.organization_id

    resp = client.request("POST", "/api/v1/decide/escalate", json_body=body)
    human = (
        f"Resolved decision {decision_id} -> option {option_id} (status: DECIDED)."
        if resp.ok
        else f"Error resolving decision {decision_id}: {resp.error}"
    )
    emit_output(resp.data, human, force_json=force_json, stdout_stream=stdout_stream)
    return resp.exit_code


def handle_decide_edit(
    args: Any,
    config: BridgeConfig,
    client: BridgeClient,
    *,
    force_json: bool = False,
    stdout_stream: Any = None,
) -> int:
    """Mutate an existing decision's title, context, options (with pros/cons), criteria, or scores in place."""
    decision_id = str(getattr(args, "decision_id", "")).strip()
    body: dict[str, Any] = {
        "action": "UPDATE",
        "decisionId": decision_id,
    }
    if getattr(args, "title", None):
        body["title"] = str(args.title).strip()
    if getattr(args, "context", None):
        body["contextSummary"] = str(args.context).strip()
    if getattr(args, "urgency", None):
        body["urgency"] = str(args.urgency).strip().upper()
    if getattr(args, "options", None):
        body["options"] = _parse_options_input(args.options)
    if getattr(args, "criteria", None):
        body["criteria"] = _parse_criteria_input(args.criteria)
    if getattr(args, "scores", None):
        body["scores"] = _parse_scores_input(args.scores)
    if config.organization_id:
        body["organizationId"] = config.organization_id

    resp = client.request("POST", "/api/v1/decide/escalate", json_body=body)
    human = (
        f"Updated decision {decision_id} in place."
        if resp.ok
        else f"Error updating decision {decision_id}: {resp.error}"
    )
    emit_output(resp.data, human, force_json=force_json, stdout_stream=stdout_stream)
    return resp.exit_code


def handle_decide_retract(
    args: Any,
    config: BridgeConfig,
    client: BridgeClient,
    *,
    force_json: bool = False,
    stdout_stream: Any = None,
) -> int:
    """Retract (archive) or hard-delete a decision so it no longer takes up space on /decide."""
    decision_id = str(getattr(args, "decision_id", "")).strip()
    hard_delete = bool(getattr(args, "hard", False) or getattr(args, "decide_action", "") in ("delete", "rm"))
    body: dict[str, Any] = {
        "action": "DELETE" if hard_delete else "RETRACT",
        "decisionId": decision_id,
        "hardDelete": hard_delete,
    }
    if getattr(args, "rationale", None):
        body["rationale"] = str(args.rationale).strip()
    if config.organization_id:
        body["organizationId"] = config.organization_id

    resp = client.request("POST", "/api/v1/decide/escalate", json_body=body)
    verb = "Deleted" if hard_delete else "Retracted"
    human = (
        f"{verb} decision {decision_id}."
        if resp.ok
        else f"Error retracting decision {decision_id}: {resp.error}"
    )
    emit_output(resp.data, human, force_json=force_json, stdout_stream=stdout_stream)
    return resp.exit_code


def handle_decide_check(
    args: Any,
    config: BridgeConfig,
    client: BridgeClient,
    *,
    force_json: bool = False,
    stdout_stream: Any = None,
) -> int:
    """Check if the active task has an open blocking decision; exits with code 2 if blocked."""
    branch = detect_git_branch(cwd=config.workspace_root, env=config.env)
    branch_state = get_branch_state(config, branch=branch)
    task_id = getattr(args, "task", None) or branch_state.get("taskId")

    params: dict[str, Any] = {"status": "OPEN"}
    if task_id:
        params["blockedTaskId"] = task_id

    resp = client.request("GET", "/api/v1/decide", params=params)
    if not resp.ok:
        emit_output(resp.data, f"Error checking decisions: {resp.error}", force_json=force_json, stdout_stream=stdout_stream)
        return resp.exit_code

    decisions = resp.data.get("decisions", []) if isinstance(resp.data.get("decisions"), list) else []
    open_decisions = [
        d for d in decisions
        if isinstance(d, dict) and str(d.get("status", "OPEN")).upper() == "OPEN"
    ]
    open_blocking = [
        d for d in open_decisions
        if str(d.get("deliveryMode") or d.get("mode") or "BLOCKING").upper() != "ADVISORY"
        and not str(d.get("contextSummary") or "").startswith("[MODE:ADVISORY]")
    ]
    open_advisory = [
        d for d in open_decisions
        if d not in open_blocking
    ]
    if open_blocking:
        first = open_blocking[0]
        payload = {
            "ok": False,
            "blocked": True,
            "error": "BLOCKED_ON_DECISION",
            "taskId": task_id,
            "blockingDecisions": open_blocking,
            "advisoryDecisions": open_advisory,
            "message": f"Task '{task_id or 'workspace'}' is blocked by open decision '{first.get('id')}' ({first.get('title')}).",
        }
        emit_output(
            payload,
            f"BLOCKED: Open decision [{first.get('id')}] '{first.get('title')}' blocks execution.",
            force_json=force_json,
            stdout_stream=stdout_stream,
        )
        return EXIT_CONCURRENCY_BLOCK

    payload = {
        "ok": True,
        "blocked": False,
        "taskId": task_id,
        "blockingDecisions": [],
        "advisoryDecisions": open_advisory,
    }
    human_msg = (
        f"Clear: No open blocking decisions for {task_id or 'workspace'} ({len(open_advisory)} advisory decision(s) open)."
        if open_advisory
        else f"Clear: No open blocking decisions for {task_id or 'workspace'}."
    )
    emit_output(
        payload,
        human_msg,
        force_json=force_json,
        stdout_stream=stdout_stream,
    )
    return EXIT_OK
