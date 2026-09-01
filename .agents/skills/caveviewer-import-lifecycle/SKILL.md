---
name: caveviewer-import-lifecycle
description: "Diagnose and change CaveViewer map-import and cache-build lifecycle behavior. Use for cache locks, pause or resume checkpoints, cancellation, partial caches, viewer shutdown, import progress, or Map Library recovery; not for general render-loop performance or packaging."
---

# CaveViewer import lifecycle

Protect user maps and the last valid cache while keeping cancellation,
pause/resume, and UI recovery bounded and observable.

## Trace the complete lifecycle

1. Read the startup and map-import sections of
   `docs/development/architecture.md`, the import model in
   `docs/development/rendering.md`, and the compatibility paths in
   `docs/development/repository-layout.md`.
2. Read the applicable `src/AGENTS.md`, `src/caveviewer/core/AGENTS.md`, and
   `src/caveviewer/gui/AGENTS.md` before editing those areas.
3. Trace ownership across `caveviewer.app`, `gui.import_controller`,
   `gui.import_process`, `core.map.cache_build_lock`,
   `core.chunking.staging`, the cache builder, viewer shutdown, and Map Library
   recovery. Do not fix only the first visible symptom.
4. Reproduce from logs or an isolated fixture. Separate an intentionally bad
   source/cache from the workflow defect that prevents recovery or cleanup.

## Preserve lifecycle invariants

- One active builder owns a cache target. Lock acquisition and release must be
  deterministic across success, failure, pause, cancellation, repeated close,
  and process exit.
- Build into staging and publish `_cache` atomically. Never expose partial work
  as a valid cache or replace the last valid cache before publication succeeds.
- Publish resumable checkpoints coherently and consume them only when their
  identity and inputs match. Treat the cache/checkpoint format as a public
  compatibility boundary.
- A user close request must reach a safe pause or cancellation point within a
  few seconds. Do not block a GUI owner thread while waiting for worker or
  process cleanup.
- Initialize callback-visible viewer state before native events can arrive;
  repeated close and cancellation requests must be idempotent.
- Recover the existing application root and visibly restore Map Library before
  showing a generic failure modal. Keep diagnostic details copyable without
  exposing implementation exceptions as the primary message.

## Implement and verify

Prefer the smallest state-machine or ownership correction at the responsible
boundary. Use temporary directories and processes in tests; never experiment
destructively on a user's map or cache.

Run focused coverage for the changed layers, including as applicable:

```text
tests/unit/gui/test_import_controller.py
tests/unit/gui/test_import_process.py
tests/unit/gui/test_import_progress_panel.py
tests/unit/gui/test_viewer_window.py
tests/unit/gui/test_map_library_workflow.py
tests/unit/core/test_cache_build_lock.py
tests/integration/test_managed_cache_streaming.py
```

Exercise success, injected failure, pause, resume, repeated cancellation,
process termination, stale-lock cleanup, partial-output cleanup, and Map Library
recovery. Bound waits with events and timeouts, then run the repository's
standard validation.
