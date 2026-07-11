# Core instructions

These rules supplement the repository-level `AGENTS.md` for files under
`src/caveviewer/core/`.

- Keep `caveviewer.core` independent of `caveviewer.gui`, Tk, and concrete OpenGL APIs. Inject
  callbacks or small interfaces at the boundary when render-thread work is
  required.
- Prefer pure functions for scheduling, budgeting, parsing policy, and spatial
  calculations. Keep filesystem, environment, and thread orchestration at the
  edges.
- Treat large-map behavior as a primary constraint. Avoid whole-file reads,
  unbounded queues, and temporary arrays proportional to the entire expanded
  mesh unless the design explicitly accounts for their memory cost.
- Cache writes must use private staging locations and clean them after every
  failure, including cancellation, `ENOSPC`, and worker exceptions.
- Cache format changes require an explicit version decision, validation tests,
  and a documented compatibility or rebuild path.
- Keep worker state transitions synchronized. Renderer callbacks are external
  operations: commit internal loaded/unloaded state only at the transaction
  point defined by the callback contract.
- Put focused tests in `tests/unit/core/`; add an integration test when thread,
  filesystem, or parser/cache boundaries interact.
