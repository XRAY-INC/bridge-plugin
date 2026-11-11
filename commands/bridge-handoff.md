---
description: Release active scope claims and record a structured turn handoff in Bridge
argument-hint: "<handoff summary>"
---

Close the current coding session cleanly in Bridge:

1. Release all active cross-worktree file scope claims held by this agent (`bridge release --all`).
2. Record a concise handoff summarizing what shipped, current branch/PR state, and recommended next steps (`bridge handoff "<summary>"`).
3. If any offline seams are queued in `.bridge/spool.jsonl`, run `bridge seam flush`.
