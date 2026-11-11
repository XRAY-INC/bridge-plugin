---
name: bridge-afk
description: Launch unattended Away From Keyboard (AFK) coding runs powered 100% by Bridge (`brg` / `bridge`) with 7-day organizational context gating, session-reloaded Organization Custom Instructions, off-branch Git savepoints (`brg savepoint`, `brg undo`, `brg rollback`), frozen file locks (`brg freeze`), isolated Git worktrees (`brg worktree`), cross-worktree scope claims (`brg claim`), autonomous 1-Click Decision Cards (`brg decide propose`), verification loops, and a structured run receipt (`AFK_RECEIPT.md`). Use when the user says "/afk", "/bridge-afk", "step away", "run unattended", or wants autonomous execution with safety guardrails.
---

# Bridge Unattended AFK Protocol (`/bridge-afk` · `/afk`)

When the user invokes `/bridge-afk` or `/afk` (or asks you to work autonomously while they step away), execute this complete 6-phase loop using **`brg`** (`bridge`). Everything—savepoints, undo/rollback, frozen files, worktrees, scope claims, 1-Click Decisions, seams, and handoffs—runs through **one unified tool: `brg`**.

---

## Phase 1 — Pre-Flight Context Gate & Teammate Identity

1. **Verify Active Identity & Workspace Binding**:
   ```bash
   brg status
   ```
   - If working on a shared machine or shared login, verify or set the active teammate identity so all seams, claims, and decision cards attribute to the right person:
     ```bash
     brg user                   # Inspect current teammate identity
     brg user <alias> --session # e.g., brg user alice@acme.com --session (or alias from org roster)
     ```
   - If in a non-Git project folder without `.bridge.json`, bind the folder first:
     ```bash
     brg init --org <orgId> --project "<Project Name>" --client "<Workspace Name>"
     ```

2. **Pull 7-Day Organizational Context & Custom Instructions**:
   ```bash
   brg context --days 7 --json
   ```
   - Output a concise **🟢 Green / 🟡 Yellow / 🔴 Red Alignment Check** before mutating files:
     - Check and apply active **Organization Custom Instructions** (`customInstructions` / `.bridge/org_instructions.md`).
     - Check active tasks & spoken quotes (`FactSource` citations).
     - Check resolved/open 1-Click Decisions (`brg decide check`).
     - Check active `FleetLease` scope claims (`brg claims`).

---

## Phase 2 — Baseline Savepoint & Frozen File Invariants

1. **Create an Off-Branch Baseline Savepoint**:
   ```bash
   brg savepoint create --label "afk-baseline"
   ```
   - This records an off-branch Git snapshot under `refs/bridge-snapshots/<snap-id>` (or a directory snapshot in non-Git folders) without touching `HEAD` or `.git/index`.
   - At any point, a single step can be rewound via `brg undo` or the entire run can be rolled back via `brg rollback afk-baseline` (which rescues newly created untracked files into `.bridge/rescued/` instead of deleting them).

2. **Freeze Invariant Files**:
   - Lock files that must NOT be modified during the unattended run (e.g., existing migrations, auth contracts, `.env*`, production configs):
   ```bash
   brg freeze "prisma/migrations/**" ".env*"
   brg freeze --list
   ```
   - The `brg guard` `PreToolUse` hook automatically blocks any `Edit`, `Write`, `MultiEdit`, or destructive `Bash` command targeting frozen globs.

---

## Phase 3 — Worktree Isolation & Cross-Worktree Scope Claims

1. **Isolate When Running Parallel Tracks**:
   - If another agent is active in the main checkout or you are fanning out parallel subagents, create an isolated Git worktree (which automatically copies `.bridge.json`):
   ```bash
   brg worktree create <slug>
   ```
   - Or start directly from a Bridge task ID:
   ```bash
   brg tasks start <taskId> --worktree
   ```

2. **Claim File / Directory Scope**:
   - Reserve the globs you will edit so concurrent agents across worktrees cannot collide:
   ```bash
   brg claim "src/<module>/**" "tests/<module>/**" --ttl 45m --reason "AFK run: <goal>"
   ```
   - If `brg claim` exits with code `2` (`LEASE_CONFLICT`), do **not** overwrite the other agent's files—choose a non-overlapping track or wait.

---

## Phase 4 — Autonomous Execution, Step Savepoints & 1-Click Decision Cards

1. **Atomic Step Execution**:
   - Break the goal into small, verifiable steps.
   - Before risky refactors, capture an explicit savepoint (`brg savepoint create --label "before-<step>"`).
   - If an edit breaks the build or tests and cannot be fixed cleanly within 2 attempts, rewind immediately with `brg undo` rather than compounding broken state.

2. **Handling Architectural Forks & Stakeholder Trade-Offs (`brg decide propose`)**:
   - **Never stall silently or guess on destructive changes.**
   - **Safe Recommended Default Exists (`--mode advisory` — Default-Unless-Vetoed)**:
     Dispatch an advisory 1-Click Decision Card so stakeholders (`--to` handles or emails from `brg context`) get 1-Click Magic Links in Slack/Email while you continue executing the recommended option:
     ```bash
     brg decide propose \
       --to "alice,bob" \
       --mode advisory \
       --title "<Decision Title>" \
       --context "<Why this fork matters>" \
       --options "Option A (Recommended),Option B (Alternative)" \
       --criteria "Reliability:9,Speed:8"
     ```
   - **Irreversible / Destructive Gate (`--mode blocking`)**:
     If a choice is destructive or irreversible (e.g., dropping a production column, breaking a public API contract), propose with `--mode blocking`, log `brg seam friction "..."`, and pause that track cleanly.

---

## Phase 5 — Verification Loop

1. Run the project's verification suite (`pytest`, `npm test`, `npx vitest run`, `npx tsc --noEmit`, `npm run build`).
2. If verification fails:
   - Fix the root cause and re-verify, OR
   - Run `brg undo` to restore the last known-good savepoint and log the blocker via `brg seam friction "..."`.
3. Once verified on a bound task, record the two-sided verification fact:
   ```bash
   brg tasks verify <taskId> "Verified <test count> tests passing and build clean."
   ```

---

## Phase 6 — Turn Closure, Scope Release & `AFK_RECEIPT.md`

1. **Log High-Signal Seams & Handoff**:
   ```bash
   brg seam win "<Concise summary of what shipped and was verified>"
   brg handoff "<Summary of what shipped, verification proof, and next steps>"
   ```
2. **Unfreeze Temporary Locks & Release Scope Claims**:
   ```bash
   brg unfreeze --all
   brg release --all
   ```
3. **Write `AFK_RECEIPT.md`** (in `.bridge/AFK_RECEIPT.md` or workspace root if requested) summarizing:
   - **Goal & Alignment Check** (`🟢` / `🟡` / `🔴`)
   - **Baseline Savepoint ID** (`snap_...`) & **Commits / Files Modified**
   - **Verification Results** (exact test/build commands and pass counts)
   - **1-Click Decisions Dispatched** (IDs, modes, recipients)
   - **Seams & Handoff Logged**
   - **Rollback Command** (`brg rollback <baseline-snap-id>`)
