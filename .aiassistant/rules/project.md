# CaveViewer project rules

Apply these rules to every JetBrains AI Assistant conversation for this
project.

- Read and follow the root `AGENTS.md` and the nearest scoped `AGENTS.md` before
  editing files.
- Treat `docs/development/` as the canonical architecture, repository-layout,
  coding, and testing reference.
- Preserve unrelated working-tree changes. Keep behavior changes separate from
  mechanical file moves and formatting-only edits.
- Do not create the proposed `src/caveviewer` tree piecemeal; follow the staged
  migration in `docs/development/repository-layout.md`.
- Add regression and failure-cleanup tests for behavior changes, run focused
  tests and the complete suite when practical, and inspect the final diff.
- Never expose or commit private keys, credentials, downloaded maps, caches,
  virtual environments, coverage data, or build artifacts.
