---
name: bridge-context
description: Pull 7-day organizational context (tasks with speaker quotes & FactSource citations, session-reloaded Organization Custom Instructions, resolved/pending decisions, active FleetLeases, seams, and ORG/TEAM Morning Briefs + 72h corrections) via bridge_context_pull or brg context before substantive work, and return a 🟢 Green / 🟡 Yellow / 🔴 Red Alignment Check.
---

# Bridge Context Gate (`bridge-context`)

Before beginning any substantive coding, planning, or architectural change in a repository connected to **Bridge** (`.bridge.json` or `BRIDGE_API_KEY`), run the **Bridge Context Gate**.

---

## 1. How to Pull Organizational Context (Subagent or Direct Call)

Prefer delegating context hydration to a lightweight subagent so raw JSON stays out of your main context window, or invoke the MCP tool / CLI directly:

- **MCP Tool (Preferred):**
  Call `bridge_context_pull` with `{"days": 7}` (and optional `"team": "<slug>"`, `"repo": "<owner/name>"`).
  If you also need detailed Morning Brief sections, call `bridge_brief_latest`.
- **CLI Fallback:**
  ```bash
  brg context --days 7 --json
  ```

---

## 2. Mandatory Verification Rule: Spoken Claims vs. Verified Proof

Bridge reconciles work across meetings, chat, code, and trackers. You **MUST** distinguish uncorroborated talk from verified completion proof:

| Classification Tier | API Field Value | Meaning | Required Agent Behavior |
| :--- | :--- | :--- | :--- |
| **Spoken Claim (Uncorroborated)** | `railClassification: "SPOKEN_CLAIM_RAIL_2"` (`verificationTier: "TIER2_CANDIDATE"`, `treatAsDone: false`) | Mentioned in a meeting transcript or team chat (`speakerQuote`), but **uncorroborated** by a merged PR, commit, or customer artifact. | **NEVER treat uncorroborated talk as done.** Do not skip implementation, close the task, or assume the feature exists in `main` until corroborated by code/test proof. |
| **Verified Proof** | `railClassification: "VERIFIED_PROOF_RAIL_3_5"` (`verificationTier: "TIER1_READY" \| "TIER0_AUTO"`, `treatAsDone: true`) | Corroborated by a merged pull request, commit SHA, active branch benchmark, or verified outbound deliverable with `FactSource` citations. | Safe to treat as verified/shipped context and build upon. |

Always inspect each task's 1-2 sentence verbatim `speakerQuote` and `citations` (`FactSource` provider + deep link) when referencing why a task is in its current state.

---

## 3. Non-Negotiable Governance Constraints

1. **Organization Custom Instructions (`customInstructions`):**
   - When `customInstructions.enabled` is `true` and `customInstructions.markdown` is non-empty, Bridge delivers organization-wide agent rules configured by workspace admins (`/settings#custom-instructions`), reloaded every session (and cached locally at `.bridge/org_instructions.md`).
   - **Follow all active `customInstructions` throughout the session** unless a rule would violate a hard security boundary or explicit user override.
2. **Resolved Decisions (`resolvedDecisions` / `status: "DECIDED"`):**
   - Every resolved decision includes a `winningOption` and weighted `criteria` (`doNotViolate: true`).
   - **Never violate a resolved decision.** If new evidence invalidates a resolved decision, escalate a new decision card via `bridge decide propose` rather than silently diverging.
3. **Pending Decisions (`pendingDecisions` / `status: "OPEN" | "IN_REVIEW"`):**
   - If your target task is `BLOCKED_ON_DECISION` or has an open blocking decision, **stop** and surface the decision options to the user.
4. **Active Scope Leases (`activeLeases`):**
   - Check `claimedGlobs` and `agentId` on active `FleetLease` locks. Never edit files matching another active agent's claimed globs.
5. **Latest `ORG` & `TEAM` Morning Briefs + 72-Hour `Correction`s (`orgBrief`, `teamBrief`, `corrections`):**
   - Treat active 72-hour `Correction`s (`PENDING`, `APPLIED`, `PROMOTED`) as authoritative overrides over older statements.

---

## 4. Required Output Format: Concise Brief + Alignment Check

Always open your context report (or the first response after pulling context) with a **🟢 Green / 🟡 Yellow / 🔴 Red Alignment Check**:

```markdown
### Bridge Alignment Check: [🟢 Green | 🟡 Yellow | 🔴 Red]
- **Tasks & Proof:** [Active task binding + any relevant tasks; explicitly flag any uncorroborated spoken claims (`SPOKEN_CLAIM_RAIL_2`) vs. verified proof (`VERIFIED_PROOF_RAIL_3_5`) with speakerQuote & citation]
- **Org Custom Instructions:** [Active (`every_session` / `hourly`) + 1-line summary of key org rules, or "None configured"]
- **Decisions:** [Any resolved decisions constraining this work (`winningOption`), or open blocking decisions]
- **Scope Leases:** [Active file globs claimed by other agents, or "Clear"]
- **Morning Brief & 72h Corrections:** [1-line takeaway from latest ORG/TEAM brief + any active 72h corrections]
```

### Alignment Severity Criteria
- **🟢 Green:** No blocking open decisions, no conflicting `FleetLease` file locks on target files, and no violated `DECIDED` or `customInstructions` constraints.
- **🟡 Yellow:** Non-blocking open decisions exist, other agents hold leases in adjacent modules, a dependency relies on an uncorroborated `SPOKEN_CLAIM_RAIL_2` spoken claim, or an active 72-hour `Correction` modifies recent context.
- **🔴 Red:** Target task is `BLOCKED_ON_DECISION`, a `CRITICAL`/`HIGH` decision blocks the requested change, the user's request directly contradicts a `DECIDED` winning option, or another agent holds an overlapping `FleetLease` on the target files. **Pause and resolve before editing code.**
