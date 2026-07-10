# Architecture

This document describes the current architectural boundaries. The proposed
directory migration is documented separately in
[repository-layout.md](repository-layout.md); a target path does not exist until
the corresponding migration is implemented.

## Dependency direction

```text
caveviewer.py
    ├── core       parsing, cache construction, streaming policy
    └── gui        dialogs, viewer, rendering, platform integration
          └── core
```

`core` must not import `gui`. GUI code may call core services, but concrete Tk
and OpenGL work stays in the GUI layer. Platform behavior is selected through
`gui/platform` adapters.

## Startup and map import

The root entry point discovers a supported model and dispatches it to the OBJ
or GLB parser. Parsers produce CPU-side mesh and material data. `core/chunker.py`
partitions that data and builds a cache in a private staging directory. Only a
complete cache is published to `_cache`; failures must remove staging output and
preserve any previously valid cache.

The cache manifest records chunk metadata, spatial bounds, material references,
and cross-section cache information. A cache-format change must either remain
backward compatible or increment its version and force a deliberate rebuild.

## Runtime streaming

`core/streaming_world.py` coordinates worker lifecycle and render-thread
callbacks. Supporting modules own focused policy:

- `core/hardware_memory.py`: RAM/GPU detection and target parsing.
- `core/streaming_budget.py`: chunk-size estimation and residency limits.
- `core/streaming_scheduler.py`: ready backlog, spatial selection, and eviction.

Workers load and prepare CPU payloads. The viewer performs OpenGL uploads and
unloads on the render thread. Internal residency state and external GPU state
must remain transactionally consistent when callbacks fail.

The longitudinal cross-section overlay has an independent worker, request
coalescing, and profile cache. It reads the positions-only cross-section cache
when available and falls back to full render chunks for older caches.

## UI and platform boundaries

Tk dialogs should keep validation and workflow state in testable controller or
model modules. `gui/platform` contains OS-specific focus, update, and system
integration behavior. Unsupported platforms use the default adapter.

Tk and OpenGL objects are main-thread resources. Background threads may parse,
read, decode, and prepare bytes, but may not mutate widgets or create/release GL
objects.

## Updates and release assets

`updates/` is a published data surface used by installed applications through
raw repository URLs. Its platform and architecture paths are compatibility
contracts. The public verification key under `security/` is bundled with the
application; private signing material must never enter the repository.

Build, package, publish, and manifest-generation workflows live under
`scripts/`. `CaveViewer.spec` and several platform scripts currently assume the
root module/resource layout, so package migration must update and test these
consumers together.
