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

## Guided Dive centerline refinement

Guided Dive keeps the centerline generator and local geometric refinements
replaceable. `core.navigation.curvature` profiles any 3D route polyline and
labels contiguous high-curvature regions with map-relative ranks from 0 to 100.
`core.navigation.voxel_volume` rasterizes cached triangle surfaces into bounded
local voxel fields. During cache construction, `core.navigation.voxel_cache`
reuses the already-written chunk files to build a tiled atlas for every cell in
each navigable cave component. Curvature-ranked regions remain diagnostic
metadata, while the atlas also covers the approach before a bend and the
straight sections after it. The current V10 sidecar requests 1 m voxels for
the whole-cave atlas and bounded fine refinement tiles, but its per-tile
capacity guard may silently increase the coarse-tile voxel size on large maps.
It therefore does not guarantee a globally isotropic 1 m field. It also stores
a compact 2 m mesh-derived route graph built by a seeded free-space flood:
voxel probes nominate inside-space candidates and every accepted neighbour
edge must pass the cached triangle-mesh guard. Only the shortest certified
path to the selected reachable route hint is persisted; the complete
cache-time flood is discarded. Fine tiles are seeded along that mesh path, so
local evidence follows the production route rather than an unrelated
centerline.
Per-tile limits keep memory and sampling work bounded on consumer hardware.
Compact route volume summaries are stored in the manifest. New caches publish the complete
navigation graph and chunk descriptors in the optional `navigation_voxels.json`
sidecar, while dense tile occupancy is stored below
`navigation_voxel_chunks/`. Runtime navigation keeps the graph/index resident
and selects either the full-memory chunk backend or a bounded lazy LRU backend;
this storage seam is independent of render `StreamingWorld`. The cache-time
selector can
prefer the largest reachable cave volume while preserving an explicit
navigation-start route. The sidecar is versioned; legacy single-window models
remain readable but do not provide fine frontier coverage.

At runtime production Guided Dive requires the current cached model; it does
not silently fall back to a centerline when the graph is missing, stale, or
unsafe. This keeps replanning from rasterizing triangles on the render machine.
Filled free voxels are aggregated into bounded navigation cells with
independent X, Y, and Z keys; stacked passages therefore remain distinct
instead of collapsing into one footprint representative. Cache construction
retains that prepared true-3D heading-aware graph for voxel evidence and legacy
frontier diagnostics. Production easiest-terminal routing uses the V10
mesh-derived graph, whose local 26-neighbour edges were exact-checked offline
and are checked again before execution. Legacy frontier replanning searches the
coarse graph with
position-and-incoming-heading states, rejects reverse edges, prunes known
dead-end branches, and biases comparable routes toward higher connectivity.
It evaluates each immediate forward branch to a bounded lookahead, retains a
safe partial prefix when the consumer-hardware expansion budget expires, and
keeps a terminal branch only when no non-dead-end forward branch remains. The
branch scorer is request-scoped and explicit: viable candidates are ordered by
connectivity first, then normalized smooth forward progress, then a bounded
backtracking penalty, with logarithmic route volume as the comfort tie-breaker.
Edge costs use the same policy for turn, connectivity, clearance, and volume
terms, while route diagnostics expose every component and the active loop
policy. The default loop policy avoids revisits; `allow_forward` may admit a
non-reversing forward revisit when a caller explicitly requests it. A negative
incoming-edge alignment remains illegal in both policies.
centerline remains only the cache-generation entrance seed and footprint
geometry bounds for production graph validation; current v6 route geometry comes
from true 3D voxel centers. A separate compatibility caller may request the
legacy centerline planner explicitly, but the viewer never uses that mode. A
startup preflight defaults to the shortest known terminal and uses the same
physical-distance mesh-graph path to it before applying exact voxel and mesh
safety checks.
When that route is accepted, the
returned `fixed_route` plan is the immutable route executed by the controller;
continuous scans, speculative replans, and rolling-horizon replacement are
disabled for that dive. An explicit farthest-terminal policy remains available
for frontier diagnostics and incomplete-cache exploration. A terminal route
stops with an explicit end-of-cave
event; a continuing prefix is marked for a forward boundary replan so a large
room cannot end the dive just because it is locally deep. The exact triangle
intersection guard still accepts or rejects the selected route. If a coarse
graph edge intersects the mesh, easiest mode may use a bounded local 2 m
refinement, with a native 1 m fallback, built from persisted evidence to hand
off only to a later node on that same fixed graph spine. It cannot globally
replan into a different branch. The resulting prepared/refined segment ledger
is exact-checked before publication. If that complete ledger
cannot be proven, easiest mode fails closed; farthest/frontier-mode preflight
may publish the farthest exact-safe prefix as an incomplete handoff. It must
replan at the frontier and is never a terminal claim. Ordinary
event-driven replans receive a cooperative wall-clock budget; when it expires,
the worker reports the phase
and the owner thread hands control back to user assist instead of holding the
camera in a long search. On capable hardware, `AutoDiveContinuousScan` is a
separate always-on speculative worker: it runs the same authoritative graph and
mesh checks without the handoff deadline, keeps one forward-hemisphere result
in flight, and hands immutable results to the owner thread only after
source-sequence, start-distance, and forward-direction checks. The accepted
route remains active while that worker scans, and the controller holds at the
last rolling-clearance-safe frontier until a valid result arrives. An explicit
pacing hold also pins the route at the position used to request an
authoritative continuation when the handoff window is short; this keeps a
valid result attachable under the small camera-to-route tolerance. Late
results are re-anchored from the actual camera with a bounded retry count. The
scan remains speculative even when it is mesh-safe: a materially shorter scan
result is kept as diagnostic evidence and does not replace a longer validated
prefix. The next scan is deferred until the current route has a comparable
remaining horizon. Local mesh-safe continuation planning trims the route at
the first exact mesh failure and reuses the already-ranked local voxel route
while rebuilding its smaller graph; it does not rerun the bounded voxel search
for each mesh retry. Replan handoff filtering uses the local route/voxel scale
and bounds near-duplicate filtering by that fine scale, rather than deriving a
fixed minimum travel step from coarse graph cadence. The
separate
bounded `AutoDiveClearance` worker performs
rolling forward-horizon checks and cached voxel probes before a frontier; it
starts the authoritative replan while a safe prefix remains and holds at a
standoff if that replan is late. Parsed voxel sidecars are signature-cached for
the session, so repeated replans do not decode the whole atlas again. A
separate bounded `AutoDiveVoxelPrefetch` worker samples the current best route
horizon, materializes its navigation chunks, releases chunks outside that
horizon, and reports partial residency without blocking camera updates or the
route-planning worker. The replanner also exposes one bounded speculative slot
for legacy or fallback callers:
it evaluates the next route while the accepted route remains active, and the
owner thread accepts it only when its source plan sequence, camera start, and
forward-direction checks still match. A late or failed speculative result is
discarded without putting the diver into a loading wait or user-assist state.
Continuous-scan failures are retried on the same route and fall back to user
assist after the bounded failure count is exhausted.
Older
caches without graph metrics, missing sidecars, disabled voxel analysis, and
cache-build failures produce an explicit navigation-authority failure and ask
for a cache rebuild. The authority event records the exact reason together
with cache and graph counts. Both the voxel builder and the entire route source
remain replaceable behind the navigation seam without changing GUI or
streaming code.

When a cached route cannot be entered, `core.navigation.recovery_scan`
generates an equal-area forward 3D hemisphere rather than a narrow yaw/pitch
grid. Runtime recovery evaluates virtual center, lateral, and vertical camera
origins with four roll orientations, filters them against cached voxel surface
fields and footprint/Y bounds, and exact-checks only the best bounded set with
the chunk mesh guard. The selected probe can carry its roll into the generated
camera route; legacy caches without a vertical model do not authorize unsafe
up/down probes. The true-3D planner uses a small entrance-side no-return band
derived from horizontal footprint/voxel spacing rather than the potentially
coarsened vertical graph spacing; route progress is not monotonic, so a
heading-valid branch may move into a shallower region.

Guided Dive's opt-in JSONL blackbox records use schema version 2. Every event
retains its existing name and session identifier, while `session_started`
captures the cache fingerprint, map bounds, navigation metadata, effective
settings, coordinate frame, and algorithm-method identifiers. Background
replans use a stable `replan_id` and generation with queue, build, and total
durations; candidate decisions report bounded timing and voxel summaries.
Voxel events also record an explicit outcome such as cache hit, no triangles, no
surface samples, disabled analysis, or an exception, together with coverage
scope, tile counts, selected curvature regions, bounds, sample counts, and
filled-graph coverage. Route-selection events record the bounded lookahead
frontier, continuation distance, onward exits, dead-end classification, user
direction alignment, entrance-band policy, route volume, volume/clearance goal
metrics, and an explicit fallback reason. Rolling-clearance events record the
worker result, voxel coverage/occupied samples, safe standoff, and whether the
authoritative replan was triggered. Voxel-prefetch events record the predicted
horizon, requested/resident chunk counts, bounded chunk IDs, storage backend,
evictions/load errors, and stale-result decisions. Speculative-replan events
record the source plan sequence, lead window, pending/accepted/discarded
outcome, and whether the accepted route was kept active while planning.
Runtime frame, clamp, assist, and stop events record plan sequence, readiness,
bounded prefetch-cell samples, and actual-versus-commanded motion. While
Guided Dive is explicitly waiting for user assistance, the controller also
writes a bounded trace at up to four samples per second or one metre of
movement. Each sample contains camera pose, world and footprint cells,
displacement, speed, and readiness. The completion record summarizes distance,
net displacement, turns, pauses, guard clamps, final resume position, and the
cached voxel branch candidates annotated with the branch the diver moved toward.
Manual navigation outside an active assistance handoff is not recorded by this
blackbox.

The process boundary also installs main-thread and worker-thread exception
hooks. When Guided Dive diagnostics are enabled, application events use the
same append-locked JSONL file with `scope: "application"`. The process records
the application-to-cache binding, viewer return or launch exception, explicit
shutdown outcome, process exit, and bounded exception tracebacks. A final
`application_process_exit` record indicates that Python reached orderly
shutdown; its absence means the process was terminated before the diagnostic
shutdown path completed (for example, a native crash, `SIGKILL`, or power
loss). The application and Guided Dive writers share a per-path lock so a
worker exception cannot corrupt a navigation record.
Full meshes, triangle arrays, and voxel grids are never written to the log.

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
the minimap occupancy footprint, and optional versioned navigation summaries.
Cache-time voxel occupancy is kept in the atomically published
`navigation_voxels.json` sidecar rather than in the render manifest. A
cache-format change must either remain backward compatible or increment its
version and force a deliberate rebuild; unsupported or missing optional voxel
artifacts must never make a render cache unusable. Large cache-time navigation
builds first measure the filled-cell cardinality and aggregate graph samples
into bounded horizontal buckets while retaining the configured vertical
resolution; they do not materialize an unbounded 1 m graph metric dictionary.
The cached-mesh collision provider also streams render-chunk triangles through
a bounded LRU during this pass, so importer, voxel, and collision geometry do
not all remain resident at once. Whole-map and average-chunk triangle counts
may disable deadline-bound speculative mesh recovery, but they do not remove
the lazy exact collision guard from cache construction, fixed-route preflight,
or route certification. Large chunked maps therefore retain the same collision
authority without requiring per-map environment overrides.
For each selected V10 route, cache construction runs a bounded, goal-directed
mesh-roadmap search inside that route's footprint corridor. Up to the first
eight route points provide automatic ingress candidates, but only candidates
with cached free-space evidence and an exact-safe mesh attachment enter the
multi-source search. The primary lattice is 2 m. Local moves use its immediate
neighbors, while longer route-guided moves provide bounded shortcuts. Every
candidate edge samples intermediate voxel evidence at half-lattice spacing and
then passes an exact cached-mesh segment check. If 2 m cannot reach the actual
final route endpoint, the same bounded search retries on a 1 m lattice.
Intermediate route points remain heuristics, never terminals, and
an unreachable endpoint publishes no authoritative mesh graph. A successful
build persists only the resulting branch-free path, alongside the existing
voxel atlas, coarse graph, and separately generated fine tiles. It also
persists the bounded startup-ingress radius; runtime still proves any camera
connector with voxel and exact-mesh checks before movement.
Map load resolves the certified mesh-graph entrance asynchronously. If the
user requests Guided Dive while that bounded placement worker is still
running, the request waits for the result and then starts preflight from the
resolved pose. It must not race preflight from the temporary first-render-chunk
fallback. Camera input during the wait still cancels automatic placement and
preflight uses the user's current manual pose.
Navigation cache certification is deliberately split into independent phases.
The `artifacts` phase validates the manifest, render chunks, navigation sidecar
paths, and navigation-chunk counts without deserializing the large graph. The
`graph` phase loads and validates the authoritative prepared graph (the V10
mesh-derived graph for current caches) and coverage profile. The `route` phase
adds start-position preflight, exact graph/voxel/mesh safety, and bounded
execution simulation (with no replan requests for a fixed route); `all` runs
the complete strict sequence. Artifact certification belongs immediately after cache publication;
graph and route certification are deeper post-build or preflight checks and
must not be mistaken for a cheap GUI-startup probe.
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
