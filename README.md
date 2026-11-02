# `bridge-plugin` (`bridge` / `brg` CLI & MCP Server)

Zero-dependency, agent-native CLI and Model Context Protocol (MCP) plugin for **[Bridge (`https://bridge.xray.tech`)](https://bridge.xray.tech)**.

Designed from the ground up for engineering teams, headless AI coding agents (Claude Code, Cursor, Antigravity, Codex), and CI/CD pipelines (`GitHub Actions`, `GitLab CI`, `Railway`, `Vercel`).

---

## Key Capabilities

1. **Dual-Binary Ergonomics (`bridge` + `brg`):**
   - Primary binary: `bridge` (`~/.local/bin/bridge`).
   - Zero-collision 3-character companion alias: `brg` (avoids any `$PATH` ambiguity with Linux `iproute2` `/usr/sbin/bridge`).
   - Uses `bridge init` (instead of `bridge link`) to bind repositories cleanly.
2. **TTY vs. Non-TTY JSON Auto-Switching (`clig.dev` & `gh` Pattern):**
   - Interactive terminal (`sys.stdout.isatty() == True`): renders aligned tables.
   - Piped / Headless Agent (`sys.stdout.isatty() == False` or `--json`): emits deterministic JSON on `stdout` and routes all notices/warnings strictly to `stderr`.
3. **Semantic Exit Codes for Autonomous Loops:**
   - `0`: Success
   - `1`: API or validation error
   - `2`: **Concurrency or Governance Block** (overlapping scope lease held by another agent, or task blocked by an open `Decision`)
   - `3`: Authentication or `.bridge.json` missing
   - `4`: Accepted into local offline spool (`.bridge/spool.jsonl`) while offline
4. **Issue-Driven Branching (`bridge tasks start <taskId>`):**
   - Checks out `feat/<taskId>-<slug>` (or an isolated worktree via `--worktree`) and binds `branch -> { taskId, jobId }` in `.bridge/state.json`.
   - Subsequent `bridge seam`, `bridge decide propose`, `bridge claim`, and `bridge handoff` commands automatically inherit the active `taskId` and `jobId`.
5. **Low-Token MCP Server (`bridge-mcp`):**
   - Exposes 3 URI Resources (`bridge://context`, `bridge://decisions/open`, `bridge://tasks/active`), 3 Prompts, and 6 consolidated tools (`< 900` total schema tokens).
6. **Client-Side Idempotency & Offline Spooling:**
   - Every mutation computes a deterministic `idempotencyKey` (`sha256(org|repo|branch|commitSha|tag|summary)`), spools to `.bridge/spool.jsonl` when offline, and replays idempotently via `bridge seam flush`.

---

## Installation

### Python (`uv` / `pipx` / `pip`)

```bash
uv tool install git+https://github.com/XRAY-INC/bridge-plugin.git
# or
pipx install git+https://github.com/XRAY-INC/bridge-plugin.git
```

### Claude Code Plugin Marketplace

```bash
/plugin marketplace add XRAY-INC/bridge-plugin
/plugin install bridge@bridge-plugin
```

### One-Command Agent & Git Hook Setup

```bash
bridge setup claude       # Configures Claude Code MCP & SKILL.md
bridge setup cursor       # Configures .cursor/mcp.json
bridge setup antigravity  # Configures Antigravity MCP & SKILL.md
bridge setup codex        # Configures Codex MCP & SKILL.md
bridge setup git-hooks    # Installs prepare-commit-msg & post-commit hooks
```

---

## Quickstart

```bash
# 1. Authenticate with your Bridge API key (xrkey_v1...)
export BRIDGE_API_KEY="xrkey_v1..."
bridge auth login --api-key "$BRIDGE_API_KEY"

# 2. Bind your repository (.bridge.json)
bridge init --org org_northstar --repo northstar/web-app

# 3. Start a task branch (binds branch -> taskId in .bridge/state.json)
bridge tasks start task_104 --slug oauth-pkce

# 4. Reserve a file scope glob across worktrees
bridge claim "src/auth/*" --ttl 30m --reason "OAuth PKCE flow"

# 5. Propose or check 1-Click Decisions when hitting a trade-off
bridge decide propose \
  --title "Token Storage Strategy" \
  --context "Choose between HttpOnly cookie session vs in-memory worker token" \
  --options "HttpOnly Cookie,Web Worker Token" \
  --urgency HIGH

# 6. Log high-signal seams (supports '-' stdin piping)
bridge seam win "Implemented OAuth 2.1 PKCE verifier and unit tests"
git log -1 --pretty=%B | brg seam win -

# 7. Release claims and leave a turn handoff
bridge release --all
bridge handoff "PKCE verifier merged; next up: token rotation endpoint"
```

---

## License

MIT
