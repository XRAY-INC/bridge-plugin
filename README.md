# `bridge-plugin` (`bridge` / `brg` CLI, MCP Server & Universal Agent Skills)

Zero-dependency, agent-native CLI, Model Context Protocol (MCP) server, and Cross-Agent Skills (`agentskills.io`) SDK for **[Bridge (`https://bridge.xray.tech`)](https://bridge.xray.tech)**.

Works across **Claude Code**, **Claude Desktop**, **OpenAI Codex**, **Cursor**, **Windsurf**, and **Gemini CLI**, as well as CI/CD pipelines (`GitHub Actions`, `GitLab CI`, `Railway`, `Vercel`).

---

## Key Capabilities

1. **Universal 7-Day Organizational Context Gate (`bridge context` / `bridge_context_pull`):**
   - Pulls a single distilled 7-day organizational snapshot from `GET /api/v1/fleet/context`:
     - Active & recently verified `Task`s with 1-2 sentence verbatim speaker quotes & `FactSource` citations.
     - Strictly separates **Spoken Claims (`SPOKEN_CLAIM_RAIL_2` / `TIER2_CANDIDATE`, never treated as done)** from **Verified Proof (`VERIFIED_PROOF_RAIL_3_5` / `TIER1_READY` & `TIER0_AUTO`)**.
     - Pending & recently resolved `Decision`s with winning options & weighted criteria (`doNotViolate: true`).
     - Active `FleetLease` locks & recent `FleetSeam`s (`win`, `decision`, `friction`, `handoff`).
     - Latest `ORG` and `TEAM` Morning Briefs (`bridge_brief_latest` / `/api/v1/briefs`) plus active 72-hour `Correction`s.
     - **Session-Reloaded Organization Custom Instructions (`customInstructions`):** Automatically pulls organization-wide agent rules configured in Bridge (`Settings → Organization Custom Instructions`) and caches them locally at `.bridge/org_instructions.md` on every session start.
   - Opens every coding session with a **🟢 Green / 🟡 Yellow / 🔴 Red Alignment Check**.
2. **Organization Custom Instructions (`brg instructions`):**
   - Author once in Bridge (`/settings#custom-instructions` or `brg instructions set --file ...`) to customize how AI coding agents behave for every developer and harness in your organization at once.
   - Manage from the CLI with `brg instructions show`, `brg instructions pull`, `brg instructions set`, and `brg instructions clear`.
3. **Cross-Agent Skills Standard (`agentskills.io` / `npx skills add XRAY-INC/bridge-plugin -g`):**
   - `skills/bridge-context/SKILL.md` — Context gate, spoken-claim vs verified-proof rule, custom instructions enforcement, and `🟢/🟡/🔴` Alignment Check.
   - `skills/bridge-cli/SKILL.md` — Complete shell guide (`bridge` / `brg`, `BRIDGE_API_KEY`, `.bridge.json`, semantic exit codes `0-4`).
   - `skills/bridge-api/SKILL.md` — Direct REST API guide (`https://bridge.xray.tech/api/v1/*` with `xrkey_v1` tokens).
   - `skills/bridge-updates/SKILL.md` — High-signal write-back protocol (`bridge seam win|decision|friction`, `bridge decide propose`, `bridge handoff`).
   - `skills/bridge-afk/SKILL.md` — Unattended Away-From-Keyboard (`/afk`) run protocol with savepoints, frozen files, worktree isolation, scope claims, and `AFK_RECEIPT.md`.
   - Dual marketplace manifests (`.claude-plugin/marketplace.json`, `.claude-plugin/plugin.json`, `.agents/plugins/marketplace.json`) and `hooks/hooks.json` (`SessionStart` context gate + `PreToolUse` `brg guard`).
4. **Local Safety, Savepoints, Frozen Files & Shared-Login Identity:**
   - **Shared-Login Teammate Identity (`brg user <alias|email> [--session|--roster|--clear]`):** Resolves organization roster aliases (synced via `brg context` into `.bridge/roster.json` or configured via `teamRoster` / `emailDomain` in `.bridge.json`) on shared accounts or machines (`~/.config/bridge/credentials.json` or session-scoped `.bridge/session_user.json`) and attaches `userEmail`, `actorEmail`, and `userName` to every seam, claim, handoff, scratchpad entry, and decision card.
   - **Off-Branch Savepoints, Undo & Automatic GC (`brg savepoint`, `brg undo`, `brg rollback`, `brg savepoint prune`):** Creates deduplicated off-branch snapshots (`refs/bridge-snapshots/<snap-id>` with untracked trees stored in `.git/objects` + `.git/bridge/snapshots.json`, or content-addressed `~/.cache/bridge/snapshots` for non-Git folders — zero unpacked copies in `<repo>/.bridge/snapshots/`), automatically prunes evicted refs and legacy `.bridge/snapshots/` folders, and rewinds step-by-step without `git reset --hard` (rescuing newly created untracked files into `.bridge/rescued/<snap-id>/`).
   - **Frozen File Locks & `PreToolUse` Guard (`brg freeze`, `brg unfreeze`, `brg guard`):** Protects sensitive files (`.env*`, `prisma/migrations/**`, billing modules) and enforces cross-worktree scope claims and automatic pre-mutation savepoints before agent tool execution.
   - **Non-Git Workspace Linking (`brg init --project "<Project>" --client "<Workspace>"` / `brg link-project`):** Binds non-Git project folders to Bridge projects.
5. **Dual-Binary Ergonomics (`bridge` + `brg`) & Semantic Exit Codes (`0-4`):**
   - Primary binary `bridge` + zero-collision 3-character alias `brg`.
   - TTY renders aligned tables; non-TTY pipes or `--json` emit deterministic JSON on `stdout`.
   - Exit codes: `0` (OK), `1` (Error), `2` (Concurrency/Decision Block), `3` (Auth Missing), `4` (Offline Spooled to `.bridge/spool.jsonl`).

---

## Installation

### 0. One-Command Machine Setup

```bash
export PATH="$HOME/.local/bin:$PATH"
export BRIDGE_API_KEY="xrkey_v1..."
curl -fsSL https://bridge.xray.tech/install.sh | sh
```

### 1. Universal Agent Skills (`agentskills.io` — Claude Code, Codex, Cursor, Windsurf, Gemini CLI)

```bash
npx skills add XRAY-INC/bridge-plugin -g
```

### 2. Python CLI & MCP Server (`uv` / `pipx` / `pip`)

```bash
uv tool install --force git+https://github.com/XRAY-INC/bridge-plugin.git
# or
pipx install --force git+https://github.com/XRAY-INC/bridge-plugin.git
```

### 3. Claude Code & Codex Plugin Marketplaces

```bash
/plugin marketplace add XRAY-INC/bridge-plugin
/plugin install bridge@bridge-plugin
```

### 4. One-Command Agent & Git Hook Setup

```bash
bridge setup
bridge setup claude
bridge setup cursor
bridge setup antigravity
bridge setup codex
bridge setup git-hooks
```

---

## Quickstart

```bash
# 1. Authenticate with your Bridge API key (xrkey_v1...)
export BRIDGE_API_KEY="xrkey_v1..."
bridge auth login --api-key "$BRIDGE_API_KEY"

# 2. Bind your repository (.bridge.json)
bridge init

# 3. Pull 7-day organizational context, custom instructions & 🟢/🟡/🔴 Alignment Check
brg context --days 7 --team platform-eng --json
brg instructions show
brg briefs --json

# 4. Start a task branch and reserve file scope globs
bridge tasks start task_104 --slug oauth-pkce
bridge claim "src/auth/*" --ttl 30m --reason "OAuth PKCE flow"

# 5. Propose or check 1-Click Decisions when hitting a trade-off
bridge decide propose \
  --title "Token Storage Strategy" \
  --context "Choose between HttpOnly cookie session vs Web Worker token" \
  --options "HttpOnly Cookie,Web Worker Token" \
  --urgency HIGH

# 6. Log high-signal seams and close your turn with a handoff
bridge seam win "Implemented OAuth 2.1 PKCE verifier and unit tests"
bridge release --all
bridge handoff "PKCE verifier merged; next up: token rotation endpoint"
```

---

## License

Bridge Customer SDK License — see [LICENSE](./LICENSE).
