---
name: bridge-cli
description: Complete guide to driving Bridge from the shell using bridge / brg commands, BRIDGE_API_KEY, .bridge.json, TTY vs non-TTY JSON output, and semantic exit codes (0-4).
---

# Bridge CLI Skill (`bridge` / `brg`)

Use this skill when driving **Bridge (`https://bridge.xray.tech`)** from a terminal, shell script, or CI/CD runner using the `bridge` (or 3-character alias `brg`) CLI.

---

## 1. Configuration & Authentication Precedence

`bridge` / `brg` resolves configuration in the following order:

1. **CLI Flags:** `--url`, `--org`, `--repo`, `--api-key`, `--json`
2. **Environment Variables:**
   - `BRIDGE_API_KEY` (token format: `xrkey_v1.<payload>.<sig>`)
   - `BRIDGE_URL` (defaults to `https://bridge.xray.tech`)
   - `BRIDGE_ORG_ID`
   - `BRIDGE_REPO` (`owner/name`)
3. **Repository Binding (`.bridge.json`):** Created via `brg init`.
4. **User Credentials (`~/.config/bridge/credentials.json`):** Saved via `brg auth login` (`0600` permissions).

```bash
# Authenticate and verify key against Bridge
export BRIDGE_API_KEY="xrkey_v1..."
brg auth login --api-key "$BRIDGE_API_KEY"
brg auth whoami --verify

# Bind current repository (.bridge.json) and add .bridge/ to .gitignore
brg init
```

---

## 2. Deterministic JSON Output & Semantic Exit Codes

- When `stdout` is a non-TTY pipe (all headless agent subprocesses) or when `--json` is passed, `brg` emits **deterministic JSON on `stdout`** and routes human notices to `stderr`.
- Always inspect the process exit code:

| Exit Code | Constant | Meaning | Agent Action |
| :--- | :--- | :--- | :--- |
| `0` | `EXIT_OK` | Command succeeded | Parse JSON from `stdout`. |
| `1` | `EXIT_ERROR` | Validation or API error | Inspect `.error` and `.message` in JSON output. |
| `2` | `EXIT_CONCURRENCY_BLOCK` | **Governance / Lease Block** | **STOP.** Overlapping `FleetLease` (`409`) or task `BLOCKED_ON_DECISION`. |
| `3` | `EXIT_AUTH_MISSING` | Missing/invalid auth or `.bridge.json` | Run `brg auth whoami` or `brg init`. |
| `4` | `EXIT_OFFLINE_SPOOLED` | Queued to `.bridge/spool.jsonl` | Safe to continue; replay later with `brg seam flush`. |

---

## 3. Core Command Reference

### Broad Organizational Context & Morning Briefs
```bash
# Pull 7-day org context (tasks + speaker quotes, decisions, leases, seams, briefs & 72h corrections)
brg context --days 7 --json
brg context --days 7 --team platform-eng --json

# Fetch latest ORG and TEAM Morning Briefs + active 72-hour Corrections
brg briefs --json
brg briefs --team platform-eng --kind TEAM_ROLLUP --json

# Show local workspace binding, active branch state, local claims, and offline spool count
brg status --json
```

### Tasks & Dispatched Agent Jobs
```bash
# List workspace tasks
brg tasks list --status IN_PROGRESS --json

# Checkout feat/<taskId>-<slug> (or --worktree) and bind branch -> taskId in .bridge/state.json
brg tasks start <taskId> --slug oauth-pkce

# Submit a bounded <=280 char verification fact for P16 two-sided reconciliation
brg tasks verify <taskId> "Verified PKCE token exchange in vitest suite (14/14 passing)"

# Claim and complete dispatched agent jobs
brg jobs list --status QUEUED --json
brg jobs claim <jobId> --globs "src/auth/*" --ttl 30
brg jobs complete <jobId> --pr "https://github.com/org/repo/pull/42" --commit "$(git rev-parse HEAD)"
```

### Cross-Worktree Scope Leases & Git Worktrees
```bash
# Reserve file/directory globs (exits 2 on 409 overlap with another agent)
brg claim "src/auth/*" "src/middleware/*" --ttl 30m --reason "OAuth PKCE implementation"

# List active claims in the repository
brg claims --json

# Release this agent's claims (or --everyone to clear all repo claims)
brg release --all

# Create/list/remove isolated Git worktrees (.worktrees/<slug>)
brg worktree create oauth-pkce
brg worktree list --json
brg worktree rm oauth-pkce
```

### 1-Click Decisions (`decide`)
```bash
# List open or resolved decisions
brg decide list --status OPEN --json
brg decide show <decisionId> --json

# Check if current task is blocked by an open decision (exits 2 if blocked)
brg decide check --json

# Propose an architectural or product trade-off card
brg decide propose \
  --title "Session Token Storage" \
  --context "Choose between HttpOnly cookie session and Web Worker memory token" \
  --options "HttpOnly Cookie,Web Worker Token" \
  --criteria "Security:10,UX Latency:8" \
  --urgency HIGH

# Score an option (1-10) or resolve a decision
brg decide score <decisionId> --option 0 --criterion 0 --score 9 --rationale "Zero XSS surface"
brg decide resolve <decisionId> --option <optionId> --rationale "Approved HttpOnly cookie"
```

### Interstitial Seams, Scratchpad & Handoffs
```bash
# Log high-signal seams (win, decision, friction, handoff)
brg seam win "Shipped SAML 2.0 metadata parser with 12 unit tests"
brg seam decision "Adopted HKDF-SHA256 per-tenant key derivation"
brg seam friction "Third-party sandbox rate-limits OAuth token endpoint to 10 req/min"

# Edit, retract, list, or flush offline spooled seams
brg seam edit last "Updated summary with PR #418 benchmark numbers"
brg seam retract last
brg seam list --limit 20 --json
brg seam flush --json

# Read/write shared working-memory scratchpad notes and close turn with a handoff
brg scratchpad read --json
brg scratchpad write "auth-architecture" "Using HKDF-SHA256 per tenant."
brg handoff "Completed SAML staging handler; next step: SCIM webhook verification"
```
