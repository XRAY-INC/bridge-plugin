---
name: bridge-api
description: Guide for scripts, CI pipelines, and integrations calling https://bridge.xray.tech/api/v1/* directly with xrkey_v1 API tokens.
---

# Bridge HTTP API Skill (`https://bridge.xray.tech/api/v1/*`)

Use this skill when writing scripts, webhooks, CI/CD steps, or custom integrations that call the **Bridge REST API** directly over HTTPS using `xrkey_v1` API keys.

---

## 1. Authentication & Standard Request Headers

Every `xrkey_v1` token (`xrkey_v1.<payloadB64>.<hmacSigB64>`) is cryptographically bound to an `organizationId` and a set of scopes (`fleet:read`, `fleet:write`, `tasks:sync`, `decide:read`, `decide:write`, `briefs:read`).

Include these headers on every request:

```http
Authorization: Bearer xrkey_v1.<payload>.<signature>
x-organization-id: <org_id>
Content-Type: application/json
Accept: application/json
x-idempotency-key: <deterministic-sha256-hex>
```

> **Note:** `x-api-key` is also accepted as an alias for `Authorization: Bearer <token>`.

---

## 2. Core Endpoints Reference

### 2.1 Distilled 7-Day Organizational Context
- **`GET /api/v1/fleet/context?days=7&team=<slug>&repository=<owner/name>`**
  Returns a single JSON payload containing:
  - `alignment`: `{ status: "GREEN" | "YELLOW" | "RED", badge: "🟢 Green" | "🟡 Yellow" | "🔴 Red", reasons: string[] }`
  - `customInstructions`: `{ enabled: boolean, markdown: string, reloadPolicy: "every_session" | "hourly", contentHash: string | null, updatedAt: string | null, updatedBy: string | null }`
  - `tasks`, `activeTasks`, `recentlyVerifiedTasks`: Each task includes `speakerName`, 1-2 sentence verbatim `speakerQuote`, `citations` (`FactSource[]`), `railClassification` (`"SPOKEN_CLAIM_RAIL_2"` with `treatAsDone: false` vs. `"VERIFIED_PROOF_RAIL_3_5"` with `treatAsDone: true`), and `verificationTier`.
  - `decisions`, `pendingDecisions`, `resolvedDecisions`: Each decision includes `winningOption`, weighted `criteria`, `options`, `rankings`, and `doNotViolate` (`true` when `status === "DECIDED"`).
  - `activeLeases`: Unexpired `FleetLease` file locks (`claimedGlobs`, `agentId`, `worktreeBranch`, `expiresAt`).
  - `seams` & `seamsByTag`: Recent 7-day `FleetSeam`s grouped by `win`, `decision`, `friction`, and `handoff`.
  - `orgBrief`, `teamBrief`, `corrections`: Latest `ORG` (`DAILY_BRIEF` / `ACCOMPLISHMENT_DIGEST`) and `TEAM` (`TEAM_ROLLUP`) Morning Briefs plus active 72-hour `Correction`s (`hoursRemaining`, `authorName`, `targetFactStatement`).

```bash
curl -sS "https://bridge.xray.tech/api/v1/fleet/context?days=7&team=platform-eng" \
  -H "Authorization: Bearer $BRIDGE_API_KEY"
```

### 2.2 Scoped Morning Briefs & 72-Hour Corrections
- **`GET /api/v1/briefs?team=<slug>&kind=DAILY_BRIEF`**
  Returns `{ ok: true, orgBrief, teamBrief, briefs, corrections }`.
- **`POST /api/v1/briefs/generate`**
  Body: `{ "kind": "DAILY_BRIEF" | "ACCOMPLISHMENT_DIGEST" | "TEAM_ROLLUP", "format": "TEXT" | "AUDIO", "membershipId": "<optional>" }`.
- **`GET | POST | PATCH /api/v1/briefs/corrections`**
  - `POST`: `{ "targetFactId": "<factId>", "targetBriefId": "<briefId>", "body": "1-sentence correction" }`
  - `PATCH`: `{ "action": "APPROVE" | "REJECT" | "PROMOTE", "correctionId": "<id>" }`

### 2.3 Tasks & Two-Sided Fact Verification
- **`GET /api/v1/tasks`** — List workspace tasks and verification states.
- **`POST /api/v1/tasks`** — Create or stage a task (`title`, `description`, `externalSource`, `externalId`, `verificationTier`, `verificationProof`).
- **`PATCH /api/v1/tasks`** — Update `taskId` (`status`, `assigneeMembershipId`, `verificationTier`).
- **`POST /api/v1/ingest/facts`** — Ingest bounded (`<=280` char) facts for two-sided task reconciliation:
  ```json
  {
    "facts": [
      {
        "kind": "OUTCOME",
        "statement": "Verified SAML 2.0 staging handler in vitest suite (GH-418)",
        "confidence": 95,
        "ticketRefs": ["GH-418"],
        "repoRefs": ["northstar/web-app"],
        "source": {
          "sourceProvider": "agent_session",
          "sourceObjectId": "commit:8f4a21c"
        }
      }
    ]
  }
  ```

### 2.4 1-Click Decisions (`Decide`)
- **`GET /api/v1/decide?status=OPEN`** — List decisions with weighted criteria, `deliveryMode` (`"BLOCKING"` | `"ADVISORY"`), and computed option rankings.
- **`POST /api/v1/decide/escalate`** — Create & distribute a 1-Click Decision Card (`to` / `recipients` resolves teammate handles/names/emails, mints 72h 1-Click Magic Links, and sends rich Decision Card emails + Slack notifications):
  ```json
  {
    "title": "Cache Invalidation Strategy",
    "contextSummary": "Need sub-50ms reads across regional workers",
    "urgency": "HIGH",
    "mode": "ADVISORY",
    "to": ["alice", "bob"],
    "blockedTaskId": "task_104",
    "options": [
      { "label": "Redis PubSub", "summary": "Low latency push", "isRecommended": true },
      { "label": "Postgres LISTEN/NOTIFY", "summary": "Zero extra infra" }
    ],
    "criteria": [
      { "name": "Latency", "weight": 9 },
      { "name": "Operational Simplicity", "weight": 8 }
    ]
  }
  ```
- **`POST /api/v1/decide/guest-vote`** — Zero-login 1-Click Magic Link vote endpoint (`token`, `optionId` or `customOption: { label, summary }`, `score`, `rationale`). Auto-resolves the Decision (`status: "DECIDED"`) and unblocks waiting tasks when an `APPROVER` stakeholder votes.
- **`POST /api/v1/decide/<id>/score`** — Score an option (`optionId`, `criterionId`, `score` 1-10) or resolve (`action: "resolve"`, `chosenOptionId`, `rationale`).

### 2.5 Interstitial Seams, Scope Leases & Scratchpad
- **`GET | POST | PATCH | DELETE /api/v1/fleet/seam`**
  - `POST`: `{ "tag": "WIN" | "DECISION" | "FRICTION" | "HANDOFF", "summary": "...", "repository": "owner/name", "gitBranch": "main", "commitSha": "...", "taskId": "..." }` (15-minute sliding deduplication window).
- **`GET | POST | DELETE /api/v1/fleet/leases`**
  - `POST` (`action: "claim"`): `{ "repository": "owner/name", "worktreeBranch": "feat/x", "claimedGlobs": ["src/auth/*"], "agentId": "claude-code", "ttlMinutes": 30 }`. Returns `409 LEASE_CONFLICT` if another agent holds an overlapping glob.
  - `POST` (`action: "release"`) or `DELETE`: `{ "repository": "owner/name", "agentId": "claude-code", "all": true }`.
- **`GET | POST /api/v1/fleet/scratchpad`** — Read or upsert shared working-memory notes (`repository`, `topic`, `bodyMarkdown`).

### 2.6 Organization Custom Instructions
- **`GET /api/v1/settings/custom-instructions`** — Read the organization's shared agent custom instructions (`enabled`, `markdown`, `reloadPolicy`, `contentHash`, `updatedAt`, `updatedBy`, `canEdit`). Requires `fleet:read` (or active workspace session).
- **`PUT /api/v1/settings/custom-instructions`** — Create or update shared custom instructions (up to `16,000` characters). Requires `OWNER`/`ADMIN` session role or an API key with `fleet:write` scope:
  ```json
  {
    "markdown": "## Engineering Standards\n- Require zero-downtime migrations and Vitest coverage on every API route.",
    "enabled": true,
    "reloadPolicy": "every_session"
  }
  ```
- **`DELETE /api/v1/settings/custom-instructions`** — Clear organization custom instructions.
