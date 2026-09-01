# CaveViewer repository instructions

These instructions apply to the entire repository. They provide the general
entry point for people and agents; development-specific policy and canonical
references live under `docs/development/`.

## Instruction routing

- For any repository development task—including source, tests, documentation,
  packaging, build, release, or automation work—read
  `docs/development/AGENTS.md` completely before acting.
- Read every additional `AGENTS.md` that applies to the files in scope. A
  nearer file supplements these rules unless it declares an explicit override.
- Use the canonical development document indexed by
  `docs/development/AGENTS.md` for detailed policy. Do not copy its narrative
  into another instruction file.
- Treat user instructions as the task scope. Do not infer authority for a
  materially different change or external action.

## Session startup

Before taking repository action, every agent must:

1. Resolve and report the repository root.
2. Read this file, `docs/development/AGENTS.md`, and every other applicable
   scoped `AGENTS.md`.
3. Inspect the active branch and Git status without changing either.
4. Identify or create the active work document according to the development
   instructions.
5. State the focused and complete validation appropriate to the work.

## General working agreement

- Inspect `git status` before editing and preserve unrelated user changes.
- Make the smallest safe change that satisfies the request and keep unrelated
  behavior, moves, and formatting out of the diff.
- Resolve exact targets before destructive or difficult-to-recover operations.
  Stop and request direction when scope or authority is unclear.
- Do not discard, overwrite, stage, commit, or publish another contributor's
  work unless the user explicitly includes it in the task.
- Keep credentials, tokens, personal paths, private keys, and machine-local
  configuration out of tracked files.
- Never commit generated caches, virtual environments, coverage output, build
  artifacts, downloaded maps, or private signing keys.

## Handoff

- Report the outcome, validation performed, known limitations, and any
  remaining user or platform action.
- Keep the active work record current through verification and merge as
  required by `docs/development/AGENTS.md`.
