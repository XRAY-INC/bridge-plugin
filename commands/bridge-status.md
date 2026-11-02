---
description: Inspect Bridge repository binding, branch task state, active scope leases, open decisions, and scratchpad memory
---

Run `bridge status` (or read MCP resource `bridge://context`) to inspect:
1. Repository configuration (`.bridge.json`) and authentication status.
2. Current Git branch binding (`.bridge/state.json` -> `taskId`, `jobId`).
3. Active file scope leases across worktrees (`bridge claims`).
4. Open 1-Click Decisions and recent working-memory scratchpad notes.
