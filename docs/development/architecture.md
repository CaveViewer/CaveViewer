# Architecture

This document describes the current architectural boundaries. The filesystem
contract is documented in [repository-layout.md](repository-layout.md).

## Dependency direction

```text
caveviewer.app
    ├── caveviewer.storage_paths XDG and portable storage roots
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
private staging directory. Cache locations are selected through
`core.cache_paths` under the managed cache root; old adjacent `_cache` and
`.caveviewer_cache` directories are not auto-discovered. Chunks, the manifest,
and referenced texture assets are published in one atomic directory
transaction. Failures must remove staging output and preserve any previously
valid managed cache.

Chunk-file construction treats its configured worker count as a maximum. It
starts with one task, samples current system RAM after completed work, and
admits only one additional concurrent worker per sample while utilization is
below 80%. Unknown availability or memory pressure keeps the build at its
already-admitted concurrency, with one worker always able to make progress.

The cache manifest records chunk metadata, spatial bounds, material references,
and the minimap occupancy footprint. A cache-format change must either remain
backward compatible or increment its version and force a deliberate rebuild.
The render-chunk binary format remains at version 1: unknown manifest fields
and extra subdirectories inside a selected managed cache are ignored, while
imports write only the active cache artifacts.

## Runtime streaming

`src/caveviewer/core/streaming_world.py` coordinates worker lifecycle and
render-thread callbacks. Supporting modules own focused policy:

- `caveviewer.core.hardware_memory`: total/current RAM and GPU detection plus
  target parsing.
- `caveviewer.core.worker_config`: CPU caps and shared worker RAM admission.
- `caveviewer.core.streaming_budget`: chunk-size estimation and residency limits.
- `caveviewer.core.streaming_scheduler`: backlog, selection, and eviction.

Workers load and prepare CPU payloads. The viewer performs OpenGL uploads and
unloads on the render thread. Internal residency state and external GPU state
must remain transactionally consistent when callbacks fail.
Streaming starts one worker and considers one additional worker only after a
prepared chunk is resident in the bounded ready queue, so each memory sample
includes real decode cost. Pool growth stops when system RAM utilization
reaches 80% or availability cannot be measured and may resume if pressure
later falls.

Geometry visibility is not limited by full-resolution texture residency.
`StreamingWorld` selects chunks using spatial distance and chunk residency
budgets; oversized texture sets are handled in `TextureManager` by deriving a
decode-time maximum texture dimension from detected GPU memory, target
percentage, and unique texture count. This keeps the visible cave geometry from
collapsing to only the few chunks whose original texture tiles fit in VRAM,
while still preventing obviously oversized texture uploads.

## UI and platform boundaries

Tk dialogs should keep validation and workflow state in testable controller or
model modules. `caveviewer.gui.platform` contains OS-specific focus, update,
and system integration behavior. Unsupported platforms use the default
adapter.

Directory selection, file reveal, notifications, and idle/suspend inhibition
use the separate `DesktopServices` capability. Linux asks XDG Desktop Portal
first and falls back to Tk or `xdg-open` only when the portal is unavailable.
Long sample-map downloads request desktop notification and inhibit support
through this same capability, but the visible Sample Maps dialog suppresses
duplicate desktop notifications because it already presents progress and
completion actions. Background update downloads request notification and
inhibit support while the package is being downloaded and verified; a visible
splash suppresses duplicate desktop notifications because it already presents
the update state and actions.
Uncached map imports request idle/suspend inhibition while parsing and
building the cache. These requests remain best-effort so desktop integration
cannot break the underlying work. Portal
requests use explicit states:

```text
IDLE -> REQUESTING -> WAITING -> {COMPLETED, CANCELLED, FAILED}
```

Startup map sessions accept either a folder containing a supported map or one
direct `.glb`/`.obj` file. This keeps Linux `Exec ... %f` desktop launches and
the in-app file and folder choosers on the same import/cache path.

Linux viewer windows use GLFW 3.4. `CAVEVIEWER_WINDOW_SYSTEM=auto` prefers
X11/XWayland when `DISPLAY` is available, then retries Wayland only for a
recognized GLFW initialization/window-creation failure. This keeps source,
debugger, and AppImage launches on the same GNOME window-management path with
normal titlebar and resize decorations. Explicit `wayland` and `x11` modes
never silently switch protocols. The Wayland application ID and X11 window class
both use `io.github.kernalpanic.caveviewer`. Initial window geometry is 80% of
GLFW's primary-monitor work area in screen coordinates. Framebuffer DPI scaling
remains enabled, while duplicate X11 monitor scaling of that already-relative
geometry is suppressed during window creation.
OpenGL HUD text is rasterized at framebuffer scale for crispness, while the
always-visible right-side viewer controls use a separate responsive HUD scale
based on the current viewer surface size. That keeps maximized and AppImage
windows legible without requiring user-provided environment variables.

Tk and OpenGL objects are main-thread resources. Background threads may parse,
read, decode, and prepare bytes, but may not mutate widgets or create/release GL
objects.

## User storage

`caveviewer.storage_paths` is the platform-neutral path boundary. Linux uses
the XDG configuration, data, cache, state, and runtime roots. Advanced settings
are configuration; remembered chooser locations are state; generated map
caches are rebuildable cache data. `CAVEVIEWER_HOME` creates isolated
`config/`, `data/`, `cache/`, `state/`, and `runtime/` children for portable or
test runs. Relative XDG variables are ignored as required by the specification,
and relative CaveViewer overrides are rejected.

Migration from `~/.caveviewer/` and older `~/.caveviewer_*` files is copy-once
and non-destructive. Managed map cache keys derive from the canonical source
path without reading or hashing a multi-gigabyte map.

The GUI process owns one `caveviewer.gui.update_manager.UpdateManager`, created
by `caveviewer.app` before the splash/viewer session loop and shut down when
that loop exits. Update state is explicit and validated:

```text
IDLE -> CHECKING -> {UP_TO_DATE, AVAILABLE, IDLE on check error}
AVAILABLE -> DOWNLOADING -> VERIFYING -> READY
                |              |
                +--------------+-> FAILED -> DOWNLOADING (retry)
any non-SHUTDOWN state -> SHUTDOWN
```

Network, verification, and staging-file work runs in manager-owned workers.
The splash polls immutable snapshots and performs widget updates on the Tk
thread. The viewer and `core.streaming_world` have no update dependency, so
opening a map neither cancels a download nor introduces update UI into the
viewer. Only process shutdown cancels an unfinished download and waits for its
temporary files to be removed.

Verified packages are persisted to the user's Downloads folder. Platform
adapters only reveal them for manual handling: Finder mounts macOS DMGs
read-only and reveals the `.app`, Explorer selects the Windows payload, and
Linux asks the desktop portal to reveal it with a containing-folder fallback.
No adapter executes or installs an update.

## Updates and release assets

`updates/` is a published data surface used by installed applications through
raw repository URLs. Its platform and architecture paths are compatibility
contracts. The public verification key under `src/caveviewer/resources/` is
bundled with the application; private signing material must never enter the
repository.

Windows uses `updates/windows/<channel>.json`. Linux distribution is x86_64-only
and uses `updates/linux/x86_64/<channel>.json`. macOS uses architecture-specific
`updates/macos/<arm64|x86_64>/<channel>.json` paths. Every manifest has a
companion `.sig` file; top-level macOS manifests and signatures remain legacy
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
