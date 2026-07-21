# Core instructions

Applies to: `src/caveviewer/core/`
Inherits: `/AGENTS.md`, `/src/AGENTS.md`
Overrides: none
Validation:

```bash
.venv-dev/bin/python -m pytest -p no:cacheprovider -q tests/unit/core
```

These rules supplement the repository and source instructions for core code.
Detailed architecture is canonical in `/docs/development/architecture.md`.

## Boundary rules

- Keep `caveviewer.core` independent of `caveviewer.gui`, Tk, and concrete
  OpenGL APIs.
- Inject callbacks, queues, value objects, or small interfaces at the boundary
  when GUI or render-thread work is required.
- Prefer pure functions for scheduling, budgeting, parsing policy, validation,
  and spatial calculations.
- Keep filesystem, environment, process, and thread orchestration at the edges
  of core components.

## Data and cache safety

- Treat large-map behavior as a primary constraint. Avoid whole-file reads,
  unbounded queues, and temporary arrays proportional to the expanded mesh
  unless the design explicitly accounts for their memory cost.
- Validate file sizes, counts, offsets, dimensions, versions, manifest fields,
  and encoded payload lengths before trusting data.
- Cache writes must use private staging locations, publish atomically, and clean
  partial output after failure, cancellation, `ENOSPC`, and worker exceptions.
- Cache format changes require an explicit version decision, validation tests,
  and a documented compatibility or rebuild path.
- Changes to shared cache data must test both cache construction and runtime
  chunk streaming.

## Concurrency and resources

- Do not assume Python's Global Interpreter Lock makes shared Python or NumPy
  state thread-safe.
- Use bounded queues for worker output that can retain decoded maps, images,
  chunk payloads, or render commands.
- Keep worker lifecycle explicit: owner, startup, cancellation, exception
  propagation, shutdown, and join behavior.
- Do not call unknown callbacks while holding internal locks.
- Internal residency state and external renderer state must remain consistent
  when callbacks fail.
- CPU-side buffers must remain valid until the owning renderer has finished
  consuming them.

## Diagnostics and tests

- Document thread ownership on public APIs that are thread-affine or lifecycle
  sensitive.
- Include useful task/resource identifiers in concurrency-related logs without
  logging secrets.
- Use deterministic synchronization hooks in tests rather than timing-only
  sleeps.
- Add integration coverage when parser, filesystem, cache, worker, and runtime
  streaming boundaries interact.
