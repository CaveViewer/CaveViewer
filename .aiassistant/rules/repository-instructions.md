---
apply: always
---

# CaveViewer repository instructions

Always follow the root `AGENTS.md` and every applicable scoped `AGENTS.md`.
Those files point to the canonical architecture, testing, release, and
documentation standards; do not create a competing copy of those rules here.

Before changing repository files or repository-related external state:

1. Resolve the repository root and inspect the current branch and Git status.
2. Read the root and nearest scoped `AGENTS.md` files.
3. Create or update the active work document under root `.work/` by default;
   promote it to `docs/development/work/` only when it must be shared or kept.
4. State the applicable focused and complete validation commands.
5. Preserve unrelated changes and keep the work document current through
   verification and merge.

This file is a tracked adapter for JetBrains AI Chat. In PyCharm, configure it
as an **Always** project rule under **Settings → Tools → AI Assistant → Rules**.
Provider accounts, models, credentials, permissions, and personal IDE state
remain local and must not be committed.
