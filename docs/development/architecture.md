# Architecture

This document describes the current architectural boundaries. The filesystem
contract is documented in [repository-layout.md](repository-layout.md).

## Dependency direction

```text
caveviewer.app
    ├── caveviewer.core       parsing, cache construction, streaming policy
    └── caveviewer.gui        dialogs, rendering, platform integration
          └── caveviewer.core
```

`caveviewer.core` must not import `caveviewer.gui`. GUI code may call core
services, but concrete Tk and OpenGL work stays in the GUI layer. Platform
behavior is selected through `caveviewer.gui.platform` adapters.

## Startup and map import

The application entry point discovers a supported model and dispatches it to
the OBJ or GLB parser. Parsers produce CPU-side mesh and material data.
`src/caveviewer/core/chunker.py` partitions that data and builds a cache in a
private staging directory. Only a complete cache is published to `_cache`;
failures must remove staging output and preserve any previously valid cache.

The cache manifest records chunk metadata, spatial bounds, material references,
and the minimap occupancy footprint. A cache-format change must either remain
backward compatible or increment its version and force a deliberate rebuild.
The render-chunk binary format remains at version 1: unknown manifest fields and
extra cache subdirectories written by older releases are ignored, so those
caches remain readable while new imports write only the active cache artifacts.

## Runtime streaming

`src/caveviewer/core/streaming_world.py` coordinates worker lifecycle and
render-thread callbacks. Supporting modules own focused policy:

- `caveviewer.core.hardware_memory`: RAM/GPU detection and target parsing.
- `caveviewer.core.streaming_budget`: chunk-size estimation and residency limits.
- `caveviewer.core.streaming_scheduler`: backlog, selection, and eviction.

Workers load and prepare CPU payloads. The viewer performs OpenGL uploads and
unloads on the render thread. Internal residency state and external GPU state
must remain transactionally consistent when callbacks fail.

## UI and platform boundaries

Tk dialogs should keep validation and workflow state in testable controller or
model modules. `caveviewer.gui.platform` contains OS-specific focus, update,
and system integration behavior. Unsupported platforms use the default
adapter.

Tk and OpenGL objects are main-thread resources. Background threads may parse,
read, decode, and prepare bytes, but may not mutate widgets or create/release GL
objects.

## Updates and release assets

`updates/` is a published data surface used by installed applications through
raw repository URLs. Its platform and architecture paths are compatibility
contracts. The public verification key under `src/caveviewer/resources/` is
bundled with the application; private signing material must never enter the
repository.

Windows uses `updates/windows/<channel>.json`. Linux and macOS use
architecture-specific `<arm64|x86_64>/<channel>.json` paths. Every manifest has
a companion `.sig` file; top-level macOS manifests and signatures remain legacy
ARM64 aliases. The update client requires a valid signature before offering a
newer manifest.

Build, package, publish, and manifest-generation workflows live under
`scripts/`. The PyInstaller contract lives at
`packaging/pyinstaller/CaveViewer.spec`; all build consumers use the installed
package and the same package-resource paths. The five platform workflows may be
run independently. `All Platform Release` runs one shared test gate, packages
all five targets in parallel from one immutable source revision, and hands the
artifacts to a single finalizer. In GitHub Actions, only that finalizer creates
the release, signs manifests, and pushes release metadata, preserving one owner
for shared mutable state. The operational contract and verification checklist
live in [releases.md](releases.md).
