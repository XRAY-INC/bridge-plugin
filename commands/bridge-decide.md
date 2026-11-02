---
description: Propose, score, or inspect 1-Click Decision cards in Bridge
argument-hint: "[list | show <id> | propose <title>]"
---

Inspect open decisions or escalate an architectural trade-off to Bridge (`https://bridge.xray.tech`) using `bridge decide` (or MCP tool `bridge_decide`).

1. If no arguments or `list` is passed, run `bridge decide list` to display open decisions and their weighted option rankings.
2. If a trade-off needs escalation, structure 2–4 concrete options with pros/cons and weighted evaluation criteria, then run `bridge decide propose`.
3. If the current branch's bound task is blocked by an open decision (`exit code 2`), pause mutations on that task and surface the Decision Card ID to the user.
