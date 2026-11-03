---
name: bridge
description: Connect AI coding agents to Bridge (https://bridge.xray.tech) via the bridge / brg CLI and bridge-mcp server for 7-day organizational context gating (🟢/🟡/🔴 Alignment Check), Scoped Morning Briefs, 1-Click Decisions, multi-agent scope claims, issue-driven branching, and P16 two-sided task verification.
---

# Bridge Agent Skill (`bridge` / `brg` & `bridge-mcp`)

Use this skill whenever operating in a repository bound to **Bridge** (`.bridge.json`) or when `BRIDGE_API_KEY` is configured.

For specialized guidance, see the 4 focused skills in this repository:
- [`skills/bridge-context/SKILL.md`](../bridge-context/SKILL.md) — Session boot context gate (`bridge_context_pull` / `brg context`), `Rail 2` spoken claims vs. `Rail 3-5` verified proof, and **🟢 Green / 🟡 Yellow / 🔴 Red Alignment Check**.
- [`skills/bridge-cli/SKILL.md`](../bridge-cli/SKILL.md) — Complete shell guide (`bridge` / `brg` commands, `.bridge.json`, semantic exit codes `0-4`).
- [`skills/bridge-api/SKILL.md`](../bridge-api/SKILL.md) — Direct HTTP REST API guide (`https://bridge.xray.tech/api/v1/*` with `xrkey_v1` tokens).
- [`skills/bridge-updates/SKILL.md`](../bridge-updates/SKILL.md) — High-signal write-back protocol (`bridge seam win|decision|friction`, `bridge decide propose`, `bridge handoff`).

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

### Step 1 — Boot & 7-Day Organizational Context Gate
Pull the 7-day organizational context via MCP tool `bridge_context_pull` (and `bridge_brief_latest`) or CLI:
```bash
brg context --days 7 --json
brg status --json
```
Always distinguish **Spoken Claims (`Rail 2` / `TIER2_CANDIDATE`, never treat uncorroborated talk as done)** from **Verified Proof (`Rail 3-5` / `TIER1_READY` & `TIER0_AUTO`)**, respect resolved decisions (`status: "DECIDED"`), and open with a **🟢 Green / 🟡 Yellow / 🔴 Red Alignment Check**.

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
- **Autonomous vs. Interactive 1-Click Decision Cards (`bridge decide propose`):**
  - Inspect `teamRoster` from `bridge_context_pull` (`brg context`) to identify relevant workspace stakeholders (`handle` or `email`).
  - **In Unattended / Overnight / CI / Release Runs:** Do **not** wait for manual prompting when hitting a product, UX, or architectural trade-off:
    - **`--mode advisory` (`--advisory` / "Default-Unless-Vetoed"):** Use when a safe recommended default exists. Distributes 1-Click Magic Links (with option quick-vote buttons + Custom Counter-Proposal input) to `--to <handles-or-emails>` while allowing the run to continue with Option A unless overridden.
    - **`--mode blocking` (Default):** Use for irreversible or destructive gates (e.g., breaking production DB migration). Pauses the bound task (`BLOCKED_ON_DECISION`, exit code `2` on `brg decide check`) until a stakeholder endorses an option.
  - **In Interactive / Paired Sessions:** When you spot a cross-team or stakeholder trade-off, proactively offer in 1 line (*"Should I send an advisory 1-Click Decision Card (`--to <stakeholder> --mode advisory`) while we proceed with Option A?"*) or dispatch immediately when asked.
  ```bash
  # Blocking escalation (halts task until resolved)
  brg decide propose \
    --title "Cache invalidation strategy" \
    --context "Need <50ms reads across multi-region workers" \
    --to "sarah,ciso@acme.com" \
    --mode blocking \
    --options "Redis PubSub,Stale-While-Revalidate CDN,Postgres LISTEN/NOTIFY" \
    --criteria "Latency:9,Operational Simplicity:8,Cost:6" \
    --urgency HIGH

  # Advisory "Default-Unless-Vetoed" card for overnight / promo runs
  brg decide propose \
    --title "Staging Promo Cutover Window" \
    --context "Proceeding with Option A (Immediate Cutover) unless overridden via 1-Click Magic Link" \
    --to "sarah,ciso@acme.com" \
    --mode advisory \
    --options "Immediate Cutover (Recommended),Hold for Morning Review" \
    --criteria "Release Velocity:9,Rollback Safety:8"
  ```
- **Dual-Homed Client + Agency Workspaces (`organizationId` + `agencyOrganizationId`):**
  - When an agency/consulting engineer works inside a Client's repository (`brg init --org <client_org> --agency-org <agency_org>`):
    - **Client-Visible by Default (no flag):** `brg tasks ...`, `brg seam win|decision`, `brg handoff`, and `brg decide propose --to "client@acme.com"` go to the **Client's Bridge** so the client sees all engineering delivery and receives client Decision Cards (and `WIN`/`HANDOFF` seams automatically mirror a rollup copy to `agencyOrganizationId`).
    - **Agency-Internal (`--internal` / `--agency`):** Pass `--internal` for internal agency blockers, SOW/margin questions, or internal playbooks (`brg seam friction --internal "..."`, `brg decide propose --internal --to "kate,doc" --mode advisory ...`) so internal agency friction never leaks into the Client's Bridge.
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
  - `bridge_context`: Local status + scratchpad read/write.
  - `bridge_context_pull`: Broad 7-day org context (`days`, `team`, `repo`) from `GET /api/v1/fleet/context`.
  - `bridge_brief_latest`: Latest `ORG` and `TEAM` Morning Briefs + 72-hour `Correction`s from `GET /api/v1/briefs`.
  - `bridge_seam`: `action: "log" | "edit" | "retract" | "list"`, `tag: "win" | "decision" | "friction" | "handoff"`.
  - `bridge_decide`: `action: "list" | "propose" | "score" | "resolve" | "status"`.
  - `bridge_scope`: `action: "claim" | "release" | "list"`.
  - `bridge_job`: `action: "list" | "claim" | "complete"`.
  - `bridge_fact`: Direct bounded `<=280 char` fact ingestion for `P16` two-sided reconciliation.
