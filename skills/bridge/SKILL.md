---
name: bridge
description: Connect AI coding agents to Bridge (https://bridge.xray.tech) via the bridge / brg CLI and bridge-mcp server for interstitial seam logging, 1-Click Decisions governance, multi-agent scope claims, issue-driven branching, and P16 two-sided task verification.
---

# Bridge Agent Skill (`bridge` / `brg` & `bridge-mcp`)

Use this skill whenever operating in a repository bound to **Bridge** (`.bridge.json`) or when `BRIDGE_API_KEY` is configured.

---

## 1. Semantic Exit Codes (Always Respect in Agent Loops)

When invoking `bridge` or `brg` from a non-TTY subprocess, `stdout` is guaranteed to be deterministic JSON:

| Exit Code | Meaning | Required Agent Action |
| :--- | :--- | :--- |
| `0` | **Success** | Parse JSON response on `stdout` and continue. |
| `1` | **API / Validation Error** | Inspect `error` field in JSON output and self-correct arguments. |
| `2` | **Concurrency / Governance Block** | **STOP IMMEDIATELY.** Either another agent holds an overlapping scope lease (`LEASE_CONFLICT`) or the active task is blocked by an open `Decision` (`BLOCKED_ON_DECISION`). Do not bypass. |
| `3` | **Auth / Config Missing** | Run `bridge status` or prompt user to configure `BRIDGE_API_KEY` / run `bridge init`. |
| `4` | **Offline Spooled** | Mutation was safely queued to `.bridge/spool.jsonl` with a deterministic `idempotencyKey`. Proceed normally; run `bridge seam flush` when connectivity resumes. |

---

## 2. Standard Session Workflow

### Step 1 — Boot & Context Inspection
Read the unified context via MCP resource `bridge://context` (or tool `bridge_context`) or CLI:
```bash
brg status --json
```
Check:
- Active branch binding (`taskId`, `jobId` from `.bridge/state.json`)
- Open decisions blocking the current task (`bridge decide check`)
- Active scope leases held by other agents (`bridge claims`)
- Latest working-memory scratchpad notes (`bridge scratchpad read`)

### Step 2 — Issue-Driven Branching & Scope Reservation
When starting work on a task or queued agent job:
```bash
# Bind branch -> taskId in .bridge/state.json and check out feat/<taskId>-<slug>
brg tasks start <taskId> --slug <short-slug>

# Or claim a dispatched agent job
brg jobs claim <jobId> --globs "src/module/*"

# Claim cross-worktree file scope before multi-file edits
brg claim "src/module/*" --ttl 30m --reason "Implementing feature X"
```

### Step 3 — Governance & Interstitial Seams During Execution
- **Architectural Trade-offs (`bridge decide`):** When encountering a meaningful architectural choice or blocker requiring stakeholder sign-off, escalate a 1-Click Decision Card instead of guessing:
  ```bash
  brg decide propose \
    --title "Cache invalidation strategy" \
    --context "Need <50ms reads across multi-region workers" \
    --options "Redis PubSub,Stale-While-Revalidate CDN,Postgres LISTEN/NOTIFY" \
    --criteria "Latency:9,Operational Simplicity:8,Cost:6" \
    --urgency HIGH
  ```
- **Decisions, Blockers & Wins (`bridge seam`):**
  ```bash
  brg seam decision "Selected Stale-While-Revalidate with 30s TTL for public endpoints"
  brg seam friction "Upstream OAuth sandbox rate-limits token exchange at 10 req/min"
  brg seam win "Shipped PKCE token exchange with 100% unit test coverage"
  ```

### Step 4 — Two-Sided Task Verification & Turn Closure
```bash
# Emit bounded <=280 char verification fact for P16 two-sided reconciliation
brg tasks verify <taskId> "Verified PKCE token exchange in vitest suite (14/14 passing)"

# Release scope claims and write working-memory handoff
brg release --all
brg handoff "Completed PKCE flow; ready for staging review"
```

---

## 3. Low-Token MCP Server Reference (`bridge-mcp`)

- **Resources (`resources/read`):**
  - `bridge://context` — Repo binding, branch state, active leases, open decisions, and scratchpad notes.
  - `bridge://decisions/open` — Open decision cards with weighted rankings.
  - `bridge://tasks/active` — Active workspace tasks and queued agent jobs.
- **Tools (`tools/call`):**
  - `bridge_context`: One-shot status + scratchpad read.
  - `bridge_seam`: `action: "log" | "edit" | "retract" | "list"`, `tag: "win" | "decision" | "friction" | "handoff"`.
  - `bridge_decide`: `action: "list" | "propose" | "score" | "resolve" | "status"`.
  - `bridge_scope`: `action: "claim" | "release" | "list"`.
  - `bridge_job`: `action: "list" | "claim" | "complete"`.
  - `bridge_fact`: Direct bounded `<=280 char` fact ingestion for `P16` two-sided reconciliation.
