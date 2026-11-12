---
name: bridge-updates
description: High-signal write-back protocol for what and when coding agents should log to Bridge (bridge seam win|decision|friction, bridge decide propose on architectural/product forks, and bridge handoff at session end).
---

# Bridge Write-Back Protocol (`bridge-updates`)

Bridge is an executive and engineering signal ledger — **not** a keystroke logger. Follow this protocol to keep the signal-to-noise ratio high across Morning Briefs, the Reconciler, and multi-agent handoffs.

---

## 1. What to Write Back & When

| Trigger Event | Command / MCP Tool | Signal Standard |
| :--- | :--- | :--- |
| **Substantive Feature, Fix, or Verified Milestone Shipped** | `brg seam win "<summary>"` or `bridge_seam(action="log", tag="win", summary="...")` | Log only when a real user-facing capability, bug fix, or verified milestone passes tests. Include ticket/PR/commit context. **Never** log routine linting, typo edits, or intermediate WIP saves. |
| **Backlogged / Historical Milestone Backfill** | `brg seam win "<summary>" --at YYYY-MM-DD --historical` or `bridge_seam(action="log", tag="win", summary="...", created_at="YYYY-MM-DD", historical=true)` | Backdates `FleetSeam.createdAt` and `FactSource.sourceAt` without collapsing rapid batch entries into `revisions`, and suppresses today's Morning Brief while preserving Project Dossier & Company Memory attribution. |
| **Committed Architectural Choice or Trade-Off** | `brg seam decision "<summary>"` or `bridge_seam(action="log", tag="decision", summary="...")` | Log when making a durable design choice (e.g., schema structure, caching model, auth primitive) and state the *why* in 1-2 sentences. |
| **Significant Blocker, Upstream Bug, or Rabbit Hole** | `brg seam friction "<summary>"` or `bridge_seam(action="log", tag="friction", summary="...")` | Log when blocked >15 minutes by an external API limit, broken upstream dependency, missing credential, or environment failure so teammates don't repeat the rabbit hole. |
| **Unresolved Architectural, Product, or Release Fork Requiring Sign-Off** | `brg decide propose --title "..." --context "..." --to "alice,bob" --mode blocking\|advisory --options "..." --criteria "..."` or `bridge_decide(action="propose", to="alice,bob", mode="ADVISORY", ...)` | Escalate and distribute a **1-Click Decision Card** (minting 72h Magic Links with 1-click option buttons + Custom Counter-Proposal input) instead of guessing whenever two viable paths have material trade-offs. Use `--mode advisory` ("Default-Unless-Vetoed") during overnight/release runs to inform and invite course-correction without halting execution, or `--mode blocking` for irreversible gates. |
| **Task Verification Proof (Two-Sided Reconciler)** | `brg tasks verify <taskId> "<statement>"` or `bridge_fact(task_id="...", statement="...")` | Keep verification statements `<= 280` characters with concrete proof (test count, commit SHA, or PR number). |
| **Session End or Context Switch** | `brg release --all` then `brg handoff "<summary>"` (or `bridge_scope(action="release", all=true)` + `bridge_seam(action="log", tag="handoff", ...)`) | Always release active `FleetLease` file locks and leave a concise handoff stating what shipped and what remains next. |

---

## 2. High-Signal vs. Low-Signal Examples

### Good (High-Signal)
- `brg seam win "Shipped GET /api/v1/fleet/context with 7-day tasks, verbatim speaker quotes, FactSource citations, and 72h brief corrections (vitest 100% green)"`
- `brg seam decision "Bound /api/v1/fleet/context to require session or xrkey_v1 auth and classify TIER2_CANDIDATE tasks as SPOKEN_CLAIM_RAIL_2 (treatAsDone=false)"`
- `brg seam friction "Connection pooler rejects session-level locks in transaction mode; switched to transaction-scoped locks inside database helper"`
- `brg handoff "Context gate and universal skills shipped and tested; next agent can wire Slack interactive decision buttons"`

### Bad (Noise — Do Not Log)
- `brg seam win "Fixed missing semicolon in route.ts"`
- `brg seam win "Ran npm install"`
- `brg seam decision "Renamed variable x to taskList"`

---

## 3. Turn-Closure Checklist

Before ending your turn:
1. Run the verification suite (`npx tsc --noEmit`, `npx vitest run`, `pytest`).
2. If a substantive milestone shipped, log one `win` seam (`brg seam win "..."`).
3. Release all claimed file scope leases (`brg release --all`).
4. Leave a turn-closure handoff (`brg handoff "..."`) if work spans multiple sessions or agents.
