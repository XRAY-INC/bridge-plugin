# `bridge-plugin` (`bridge` / `brg` CLI, MCP Server & Universal Agent Skills)

Zero-dependency, agent-native CLI, Model Context Protocol (MCP) server, and Cross-Agent Skills (`agentskills.io`) plugin for **[Bridge (`https://bridge.xray.tech`)](https://bridge.xray.tech)**.

Works across **Claude Code**, **Claude Desktop**, **OpenAI Codex**, **Cursor**, **Windsurf**, and **Gemini CLI**, as well as CI/CD pipelines (`GitHub Actions`, `GitLab CI`, `Railway`, `Vercel`).

---

## Key Capabilities

1. **Universal 7-Day Organizational Context Gate (`bridge context` / `bridge_context_pull`):**
   - Pulls a single distilled 7-day organizational snapshot from `GET /api/v1/fleet/context`:
     - Active & recently verified `Task`s with 1-2 sentence verbatim speaker quotes & `FactSource` citations.
     - Strictly separates **Spoken Claims (`Rail 2` / `TIER2_CANDIDATE`, never treated as done)** from **Verified Proof (`Rail 3-5` / `TIER1_READY` & `TIER0_AUTO`)**.
     - Pending & recently resolved `Decision`s with winning options & weighted criteria (`doNotViolate: true`).
     - Active `FleetLease` locks & recent `FleetSeam`s (`win`, `decision`, `friction`, `handoff`).
     - Latest `ORG` and `TEAM` Morning Briefs (`bridge_brief_latest` / `/api/v1/briefs`) plus active 72-hour `Correction`s.
   - Opens every coding session with a **🟢 Green / 🟡 Yellow / 🔴 Red Alignment Check**.
2. **Cross-Agent Skills Standard (`agentskills.io` / `npx skills add XRAY-INC/bridge-plugin -g`):**
   - `skills/bridge-context/SKILL.md` — Context gate, `Rail 2` vs `Rail 3-5` verification rule, and `🟢/🟡/🔴` Alignment Check.
   - `skills/bridge-cli/SKILL.md` — Complete shell guide (`bridge` / `brg`, `BRIDGE_API_KEY`, `.bridge.json`, semantic exit codes `0-4`).
   - `skills/bridge-api/SKILL.md` — Direct REST API guide (`https://bridge.xray.tech/api/v1/*` with `xrkey_v1` tokens).
   - `skills/bridge-updates/SKILL.md` — High-signal write-back protocol (`bridge seam win|decision|friction`, `bridge decide propose`, `bridge handoff`).
   - Dual marketplace manifests (`.claude-plugin/marketplace.json`, `.claude-plugin/plugin.json`, `.agents/plugins/marketplace.json`) and `hooks/hooks.json` (`SessionStart` context gate reminder).
3. **Dual-Binary Ergonomics (`bridge` + `brg`) & Semantic Exit Codes (`0-4`):**
   - Primary binary `bridge` + zero-collision 3-character alias `brg`.
   - TTY renders aligned tables; non-TTY pipes or `--json` emit deterministic JSON on `stdout`.
   - Exit codes: `0` (OK), `1` (Error), `2` (Concurrency/Decision Block), `3` (Auth Missing), `4` (Offline Spooled to `.bridge/spool.jsonl`).

---

## Installation

### 1. Universal Agent Skills (`agentskills.io` — Claude Code, Codex, Cursor, Windsurf, Gemini CLI)

```bash
npx skills add XRAY-INC/bridge-plugin -g
```

### 2. Python CLI & MCP Server (`uv` / `pipx` / `pip`)

```bash
uv tool install git+https://github.com/XRAY-INC/bridge-plugin.git
# or
pipx install git+https://github.com/XRAY-INC/bridge-plugin.git
```

### 3. Claude Code & Codex Plugin Marketplaces

```bash
/plugin marketplace add XRAY-INC/bridge-plugin
/plugin install bridge@bridge-plugin
```

### 4. One-Command Agent & Git Hook Setup

```bash
bridge setup claude       # Configures .mcp.json & .claude/skills/bridge-*/SKILL.md
bridge setup cursor       # Configures .cursor/mcp.json & .cursor/skills/bridge-*/SKILL.md
bridge setup antigravity  # Configures .gemini/settings.json & .agent/skills/bridge-*/SKILL.md
bridge setup codex        # Configures .agents/skills/bridge-*/SKILL.md
bridge setup git-hooks    # Installs prepare-commit-msg & post-commit hooks
```

---

## Quickstart

```bash
# 1. Authenticate with your Bridge API key (xrkey_v1...)
export BRIDGE_API_KEY="xrkey_v1..."
bridge auth login --api-key "$BRIDGE_API_KEY"

# 2. Bind your repository (.bridge.json)
bridge init

# 3. Pull 7-day organizational context & 🟢/🟡/🔴 Alignment Check
brg context --days 7 --team platform-eng --json
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

MIT
