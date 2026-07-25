# Architecture

This document describes the current architectural boundaries. The filesystem
contract is documented in [repository-layout.md](repository-layout.md).

## Dependency direction

```text
caveviewer.app
    ├── caveviewer.storage_paths XDG and portable storage roots
    ├── caveviewer.core       preferences, discovery, import/cache, streaming policy
    └── caveviewer.gui        dialogs, rendering, platform integration
          ├── caveviewer.core
          └── caveviewer.benchmarking benchmark controller adapter

caveviewer.benchmark              direct cache/scenario benchmark CLI
    ├── caveviewer.benchmarking    scenarios, metrics, comparisons, and routes
    └── caveviewer.gui             viewer runtime adapter

caveviewer.benchmarking
    ├── map_runner                 local map benchmark CLI orchestration
    └── caveviewer.core.navigation reusable route and centerline primitives
```

`caveviewer.core` must not import `caveviewer.gui`, `caveviewer.app`, or
benchmarking. GUI, benchmark, and application entry-point code may call core
services. Benchmarking may call reusable core policy. Concrete Tk and OpenGL
work stays in the GUI layer. Platform behavior is selected through
`caveviewer.gui.platform` adapters.

`caveviewer.benchmarking` owns benchmark scenario parsing, measurement
summaries, regression comparisons, benchmark-specific route selection, and the
generic local map benchmark runner exposed as `caveviewer-map-benchmark`. It may
depend on reusable core navigation/streaming policy, but it must not own viewer
presentation or render-thread OpenGL resources. `viewer_window.py` adapts a
`BenchmarkController` into the real render loop when the benchmark CLI launches
the viewer.

## Startup and map import

Core import services discover supported models and dispatch them to the OBJ or
GLB parser. `core.map.source_model` owns source selection,
`core.map.importer` owns parse/cache orchestration, and app/GUI/CLI code adapts
those services to console or Tk progress displays. Parsers produce CPU-side
mesh and material data. `src/caveviewer/core/chunking/builder.py` partitions
that data and builds a cache in a private staging directory. Cache locations are
selected through `core.map.cache_paths`; the default generated cache directory
is `_cache` inside the source map folder. Explicit `CAVEVIEWER_MAP_CACHE_DIR`
or CLI `--cache-root` callers use hashed cache directories under that separate
root. The older `.caveviewer_cache` directory is not auto-discovered.
Chunks, the manifest, and referenced texture assets are published in one atomic
directory transaction. Failures must remove staging output and preserve any
previously valid generated cache.

First-time imports launched from the viewer run in a spawned child process
through `src/caveviewer/gui/import_process.py`. The viewer process owns OpenGL,
window events, progress rendering, and desktop idle/suspend inhibition; the
child process owns parsing, cache construction, texture staging, and cache
publication. Progress, completion, and traceback-bearing failure events cross
back to the viewer through a process queue. This keeps desktop event loops
responsive during CPU-heavy imports and isolates import crashes from the UI
process. Viewer shutdown asks the parent-side relay worker to stop, waits for a
short bounded interval, terminates any reachable active child process, and then
ignores late import messages so closed windows cannot apply stale completion
events. The child emits heartbeat events with the current stage and RAM snapshot
while it is working, runs at reduced desktop priority, and caps common native
compute-library thread counts before importing NumPy-heavy modules.
Parent-side cancellation, shutdown, or abnormal child exit cleans abandoned
private staging directories for the target cache when the child is no longer
alive.

Import preflight is intentionally early. OBJ imports count vertices, UVs,
normals, and triangulated faces before allocating large arrays; that count is
used to reject imports whose estimated peak footprint exceeds currently
available system RAM. Disk preflight runs before parsing and includes both the
source model and staged texture assets when they are known. `build_cache()`
repeats disk checks before writing so direct callers and mid-import free-space
changes remain covered.

Chunk-file construction treats its configured worker count as a maximum. It
starts with one task, samples current system RAM after completed work, and
admits only one additional concurrent worker per sample while utilization is
below 80%. Unknown availability or memory pressure keeps the build at its
already-admitted concurrency, with one worker always able to make progress.

The cache manifest records chunk metadata, spatial bounds, material references,
and the minimap occupancy footprint. A cache-format change must either remain
backward compatible or increment its version and force a deliberate rebuild.
The render-chunk binary format remains at version 1: unknown manifest fields
and extra subdirectories inside a selected generated cache are ignored, while
imports write only the active cache artifacts.

## Runtime streaming

`src/caveviewer/core/streaming/world.py` coordinates worker lifecycle and
render-thread callbacks. Runtime streaming depends on focused core policy
modules:

- `caveviewer.core.hardware.system_memory`: total/current system RAM detection.
- `caveviewer.core.hardware.gpu_memory`: active-GPU memory detection and
  fallback budgets.
- `caveviewer.core.hardware.memory_targets`: RAM and GPU utilization target
  parsing.
- `caveviewer.core.workers.allocation`: CPU caps and shared worker RAM admission.
- `caveviewer.core.streaming.budget`: chunk-size estimation and residency
  limits.
- `caveviewer.core.streaming.scheduler`: backlog, selection, and eviction.
- `caveviewer.core.textures.decoding`: worker-safe CPU texture decode,
  inspection, and texture budget selection.

Workers load and prepare CPU payloads. The viewer performs OpenGL uploads and
unloads on the render thread. Internal residency state and external GPU state
must remain transactionally consistent when callbacks fail.
On Linux, each streaming worker raises its own nice value by the configured
increment so chunk preparation yields CPU time to the GUI/render thread; a
process-wide `os.nice()` call is intentionally not used for this runtime pool.
Streaming starts one worker and considers one additional worker only after a
prepared chunk is resident in the bounded ready queue, so each memory sample
includes real decode cost. Pool growth stops when system RAM utilization
reaches 80% or availability cannot be measured and may resume if pressure
later falls.

Geometry visibility is not limited by full-resolution texture residency.
`StreamingWorld` selects chunks using spatial distance and chunk residency
budgets; oversized texture sets are handled by splitting CPU texture decode
from OpenGL texture ownership. `core.textures.decoding` derives a decode-time
maximum texture dimension from detected GPU memory, target percentage, and
unique texture count, then workers decode Pillow image data into CPU bytes.
`gui.texture_manager` consumes those decoded payloads on the render thread,
creates/reuses/releases OpenGL textures, and enforces render-thread ownership
for texture GPU work. `gui.chunk_upload` owns resident chunk GPU bookkeeping,
partial upload state, unload cleanup, and shade-mode VBO rewrites. Runtime
uploads advance through render-thread operation queues: texture allocation is
separated from row-band writes, and dense material groups are split into
triangle-aligned VBO slices whose storage reservation and data writes advance
separately where the OpenGL context supports it. Texture and VBO slice sizes
start conservatively and shrink automatically after measured upload stalls.
This keeps the visible cave geometry from collapsing to only the few chunks
whose original texture tiles fit in VRAM, while still preventing obviously
oversized texture uploads. GPU memory detection is platform-specific:
NVIDIA uses `nvidia-smi` when available, Linux AMD uses DRM sysfs, low-VRAM AMD
integrated GPUs add 50% of reported GTT/shared memory capped at 2 GB, Windows
AMD/Intel currently use an 8 GB fallback budget, and macOS currently uses a
conservative 1 GB fallback when no override is set. Texture cap selection logs
the budget inputs and the selected common dimension before the first oversized
texture is resized.

## UI and platform boundaries

Tk dialogs should keep validation and workflow state in testable controller or
model modules. `caveviewer.gui.platform` contains OS-specific focus, update,
and system integration behavior. Unsupported platforms use the default
adapter.

GUI architecture guardrails are executable. The test file
`tests/unit/gui/test_gui_architecture_boundaries.py` checks that GUI modules do
not import upward into `caveviewer.app`, that direct platform checks stay
inside `src/caveviewer/gui/platform`, and that GUI Python modules carry
ownership docstrings instead of placeholder module-path docstrings.

The splash Map Library is split by responsibility: `map_library.py` builds
presentation-independent recent-map titles, `map_library_controller.py` owns
standard-library catalog/download state, `map_library_workflow.py` owns
catalog fetches, download queue polling, cancellation, and row workflow
transitions, `map_library_panel.py` owns Tk row, scroll, status, and
overflow-menu presentation, and `splash_screen.py` wires those pieces to
session actions such as opening maps and preferences. The standard-library map
list is remote-data driven: `standard_library_maps.py` fetches the configured
GitHub release, loads the `caveviewer-map-library.v1.json` release asset when
present, joins manifest entries to release zip assets, infers rows for extra
zip assets when no manifest is present, and falls back to the last cached
catalog or bundled catalog resource when offline.

Directory selection, file reveal, notifications, and idle/suspend inhibition
use the separate `DesktopServices` capability. Linux asks XDG Desktop Portal
first and falls back to Tk or `xdg-open` only when the portal is unavailable.
Long map library downloads request desktop notification and inhibit support
through this same capability, but the visible Map Library dialog suppresses
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
the in-app folder chooser on the same import/cache path as desktop-shell direct
file launches.
`gui.map_opening` owns the shared directory chooser and selected-folder
resolution used by startup compatibility wrappers and the in-viewer Open
action, so viewer rendering code does not import upward into `caveviewer.app`.

Linux viewer windows use GLFW 3.4. `CAVEVIEWER_WINDOW_SYSTEM=auto` prefers
X11/XWayland when `DISPLAY` is available, then retries Wayland only for a
recognized GLFW initialization/window-creation failure. This keeps source,
debugger, and AppImage launches on the same GNOME window-management path with
normal titlebar and resize decorations. Explicit `wayland` and `x11` modes
never silently switch protocols. The Wayland application ID and X11 window class
both use `io.github.caveviewer.caveviewer`. Initial window geometry is 80% of
GLFW's primary-monitor work area in screen coordinates. Framebuffer DPI scaling
remains enabled, while duplicate X11 monitor scaling of that already-relative
geometry is suppressed during window creation.
OpenGL HUD text is rasterized at framebuffer scale for crispness, while the
always-visible right-side viewer controls use a separate responsive HUD scale
based on the current viewer surface size. That keeps maximized and AppImage
windows legible without requiring user-provided environment variables.
Viewer recording keeps workflow decisions in `viewer_window.py` on the render
thread. `gui.recording_capture` owns framebuffer readback resources and staged
frame draining. `gui.recording` owns ffmpeg command construction, encoder
writer/stderr workers, and asynchronous stop finalization.
`gui.recording_controller` owns recording countdowns, transient status messages,
capture timing, and dropped-frame accounting so those workflow decisions remain
testable without constructing an OpenGL window.

Tk and OpenGL objects are main-thread resources. Background threads may parse,
read, decode, and prepare bytes, but may not mutate widgets or create/release GL
objects.

## User storage

`caveviewer.storage_paths` is the platform-neutral path boundary. Linux uses
the XDG configuration, data, cache, state, and runtime roots; macOS, Windows,
and unsupported platforms currently preserve the historical `~/.caveviewer/`
root until their storage conventions are migrated separately. Preferences
are configuration; remembered chooser locations are state; generated map
caches are rebuildable cache data stored in the source map folder's `_cache`
subdirectory by default. Downloaded map-library entries are ordinary user
downloads by default, stored under the user's Downloads folder or the folder
selected by `CAVEVIEWER_MAP_LIBRARY_DIR` or Preferences, and their generated
caches live inside the downloaded map folder unless an explicit cache-root
override is set.
`CAVEVIEWER_HOME` creates isolated `config/`, `data/`, `cache/`, `state/`, and
`runtime/` children for portable or test runs, but map caches still default to
adjacent `_cache` directories. `CAVEVIEWER_MAP_CACHE_DIR` overrides only the
map-cache root for advanced runs that need generated caches on a separate
filesystem. Relative XDG variables are ignored as required by the specification,
and relative CaveViewer storage-root/cache overrides are rejected.

On Linux, migration from `~/.caveviewer/` and older `~/.caveviewer_*` files is
copy-once and non-destructive. Older app-data `map_library` and `sample_maps`
directories are moved into the configured map-library location when possible.
Explicit-root map cache keys derive from the canonical source path without
reading or hashing a multi-gigabyte map.

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
thread. The viewer and `core.streaming.world` have no update dependency, so
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
