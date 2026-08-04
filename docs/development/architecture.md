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

## Offline navigation-cache refinement

Offline navigation certification keeps the centerline generator and local
geometric refinements replaceable. `core.navigation.curvature` profiles any 3D route polyline and
labels contiguous high-curvature regions with map-relative ranks from 0 to 100.
`core.navigation.voxel_volume` rasterizes cached triangle surfaces into bounded
local voxel fields. During cache construction, `core.navigation.voxel_cache`
reuses the already-written chunk files to build a bounded atlas around one
selected terminal-route corridor. V12 divides that corridor into fixed
orthogonal chunks at 1 m X/Z by 0.25 m Y. Capacity pressure subdivides chunks rather
than coarsening them, and incomplete surface sampling fails the navigation
build. Overlapping chunks are merged on a global orthogonal grid where any
sampled surface observation overrides free evidence. The merge considers every
bounded corridor-filtered non-surface cell before selecting packed free-space
evidence, so one imperfect local seed cannot delete seam evidence.

For OBJ imports without an explicit entrance sidecar, the offline
certificate uses the first declared `v` record as the source-order entrance
anchor. The vertex is
surface evidence rather than an executable camera position, so the cache
chooses a free XYZ attachment within an immutable 24 m cap; only that certified
interior attachment is executable. A numerically sorted spatial chunk is not
entrance provenance. The builder
derives open candidates from that entrance toward both footprint-diameter
directions and recommends the longest route that survives complete voxel and
mesh certification; clearance and volume are tie-breakers.

One packed six-connected component must contain both ingress and endpoint
evidence. Surface-gap intervals propose vertical layers, but disconnected
fragments are never combined and never defer connectivity to the roadmap.
The manifest entrance selects one complete, source-connected interval chain
before the route is executable. Every ordered route-cell candidate stays in
that exact footprint cell. Entrance and final candidates must intersect their
selected bounded intervals, allowing only half a vertical voxel of Y
quantization tolerance. An intermediate cell may additionally use a free key
inside the hull of one specific, continuity-compatible adjacent interval pair;
all stacked intervals in neighboring cells are never collapsed into a shared Y
slab. A component is eligible only when it contains candidates for the
entrance cell, every intermediate cell, and the final cell. Candidate limits
are applied after component selection while retaining vertically distinct
intervals. The entrance locator may attach only to the first group, and
terminal candidates come only from the final group and remain within the
requested endpoint snap cap. A complete exact path to the real endpoint then promotes
`known_terminal_reached`. The primary extractor walks ordered surface-gap
waypoint sets with six-connected searches and one shared
expansion budget, so a long cave does not require a whole-room roadmap flood.
Each returned cardinal edge is voxel- and mesh-checked immediately; a rejected
edge is blocked and only that bounded leg is rerouted. A bounded string-pull
may smooth the resulting staircase, but every shortcut repeats the same voxel
boundary/interior samples and cached-triangle mesh guard. Only this exact-safe
path is persisted. Its first exact node becomes the published route/camera
start, and cache-time search state is discarded. An OBJ or authored ingress
never retries a later route range or publishes a suffix.

OBJ-derived centerline points supply horizontal ordering and a bounded X/Z
work envelope only. Their interpolated Y samples can land on a floor, ceiling,
or another stacked passage and therefore never filter vertical candidates or
establish connectivity. Consecutive selected surface-gap intervals define a
bounded transition sampling envelope: both non-entrance cells touched by that
specific pair and available cardinal support cells for a diagonal step are
widened, while the original entrance gap remains unchanged. This supplies the intermediate
0.25 m voxels needed to prove a steep transition without claiming that the
transition is free. The occupied-wins field, one global component, and exact
mesh roadmap remain authoritative. Capacity pressure narrows the horizontal
envelope through deterministic radii without coarsening cells, but an OBJ
route never narrows below the X/Z uncertainty of its source footprint cells.

If the evidence cannot be traversed on the 1 m X/Z by 0.25 m Y execution
lattice, one universal bounded retry uses 0.5 m X/Z by 0.25 m Y in a 4 m
horizontal route envelope over the same admitted evidence. It may widen once
to 8 m after exhaustive non-capacity failure. The retry infers no free cells:
every point maps to admitted evidence and every edge passes sampled voxel and
exact mesh checks. It visits the same ordered surface-gap waypoint groups and
shares only the coarse search's remaining node ledger; the older raw-guide
adaptive planner cannot publish a V12 production route. Up to 64 final-cell
interval-backed candidates may be exact-tested; a local neighbor shell cannot
reintroduce an out-of-interval layer. Only the exact-reachable candidate
becomes the terminal, and an ingress seed cannot count as zero-edge completion.

Metadata persists every bounded surface-gap interval and its midpoint needed
to propose stacked passage layers. Its sparse vertical surface bins are 0.25 m or finer, so a
half-metre floor-to-ceiling gap remains representable without allocating a
dense whole-height column. It never substitutes an imported or interpolated
centerline Y for missing vertical evidence. A failed fixed field or exact route still
publishes no V12 sidecar. The bounded cache-time searches read the
conservative clearance floor from selected packed evidence instead of
recomputing surface distance for every candidate. It may skip collision work
for a non-improving relaxation because the existing cost already came through
a safe edge; every improving edge remains voxel- and mesh-validated.
Per-tile limits keep memory and sampling work bounded on consumer hardware.
Compact route volume summaries are stored in the manifest. New caches publish the complete
navigation graph and chunk descriptors in the optional `navigation_voxels.json`
sidecar, while dense tile occupancy is stored below
`navigation_voxel_chunks/`. Certificate tooling can select either the
full-memory chunk backend or a bounded lazy LRU backend; this storage seam is
independent of render `StreamingWorld`. For the inferred
manifest entrance or a valid explicit entrance override, the cache-time selector chooses the longest complete
certified non-circular path, with clearance and volume as deterministic
tie-breakers. Exhausting a bounded exact search is unresolved, not proof that
the candidate is unsafe; while a longer candidate is capacity-limited, the
selector publishes no shorter recommendation. The sidecar is versioned; V11 and older supported
models remain readable for compatibility but cannot authorize current
certificate workflows.

The viewer does not execute this route data. Cmd/Ctrl+A is deliberately
unhandled, no GUI controller imports the autonomous planner, and a normal map
open starts at the first manifest chunk center. The retained graph, voxel, and
mesh-route helpers are offline certificate and developer-inspection tools: they
are used by `caveviewer-navigation-verify` and core tests, not to move a
viewer camera or adjust `StreamingWorld` policy. Recorded Dive is the only
camera-playback path and supplies its own first pose.

The navigation sidecar remains optional and additive to the render cache. Its
prepared true-3D graph, fixed voxel chunks, clearance/volume metrics, and
exact-mesh evidence preserve cache certification and compatibility checks
without adding a runtime cache rebuild, cache mutation, or background decode
to ordinary viewing.

Certificate tooling may use bounded recovery probes and local mesh/voxel
analysis to explain an unresolved route, but those results are non-executable
viewer diagnostics. The former autonomous-controller JSONL blackbox and its
cache-local writer were removed with the controller.

An explicit `Cmd/Ctrl+T` manual route trace is a separate diagnostic surface.
It samples the render-thread camera pose after movement, sends JSONL records
through a bounded queue to one background writer, and marks bookmark/minimap
teleports as discontinuities instead of counting them as flown distance.
Completed traces live under the map-local `_guided_dives` directory. Their
location is anchored to the map root rather than the generated-cache location,
so atomic cache replacement and managed-cache storage do not erase the
reference flight. They remain optional ground truth: cache construction and
offline certificate route selection never consume them as required map
metadata.

Recorded Dive is the separate trace-playback path. Opening a completed JSONL
associates its bounded source basename, cache-manifest version, chunk size,
triangle count, and versioned cache identity with the local map. Cache
construction writes that identity from a streaming SHA-256 of the source file
and a canonical SHA-256 of the completed manifest. Normal rendering remains
compatible with manifests that predate this additive field, but Guided Dive
recording and v2 playback require a rebuild when it is absent.
Normal cache validation rebuilds a stale or missing map-local cache before
viewing; playback refuses a different geometry or cache layout. The trace's
first pose replaces ordinary cache-derived viewer placement, and every pose is
applied directly on the render thread without navigation clamping, smoothing,
collision rejection, or route planning.
Map Library exposes **Open guided dive…** only when the selected map's
canonical `_guided_dives` directory has a JSONL file. Its action uses the
existing `DesktopServices` file picker, then obtains one fresh capability fact
for the selected file: the file must remain map-local, parse within the bounded
trace contract, resolve to that map's source, and match a current cache
manifest exactly. `decide_guided_dive_playback` hides a map with no trace and
otherwise fails closed with a concise disabled-state explanation. This is a
per-map action-time preflight, not a `PlatformRuntime.feature_gates` entry;
startup repeats the manifest validation at the viewer boundary to cover a
filesystem change after splash has closed.
Position and orientation are interpolated by trace time; a declared
discontinuity remains an instantaneous jump. `StreamingWorld` receives a
bounded chronological lookahead tube, and the playback clock freezes whenever
the next pose's local render chunks are not GPU-resident. This makes trace time
independent of render frame rate while allowing slower hardware to buffer
without skipping part of the recorded flight.

The process boundary also installs main-thread and worker-thread exception
hooks. `ApplicationDiagnostics` is a generic optional sink with no output path
until an explicit consumer binds one, so ordinary viewer sessions do not create
cache-local diagnostic files. It can record lifecycle and bounded exception
context without changing cache construction, navigation-sidecar contents, or
manual tracing.


## Startup and map import

Core import services discover supported models and dispatch them to the OBJ or
GLB parser. `core.map.source_model` owns the immutable source-format registry,
source selection, and selected-format capability facts; that registry is the
release-policy source of truth for discovery, map-picker guidance, and Linux
package metadata validation. `gui.features.decide_map_source_import` turns one
selected descriptor's capability into an action-time decision before the GUI
accepts an import. It is intentionally not stored in
`PlatformRuntime.feature_gates`: the released format list is static, but the
user-selected descriptor and its required companion assets vary per action.
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
the minimap occupancy footprint, optional versioned navigation summaries, and
an additive `guided_dive_identity` when source hashing succeeds. This identity
is produced during cache construction, not while the render thread starts a
manual trace. Existing cache manifests stay renderable; they must be rebuilt
before the versioned Guided Dive contract can record or replay against them.
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
not all remain resident at once. It pre-indexes render-chunk AABBs in spatial
buckets and applies the existing exact AABB and triangle tests only to nearby
chunks; bucket-boundary contacts remain inclusive, and oversized queries fall
back to the complete chunk list. Fixed-tile rasterization applies its local
triangle-AABB prefilter as one vectorized mask while preserving source order,
surface sampling, occupied cells, and truncation limits. Whole-map and
average-chunk triangle counts
may disable optional bounded recovery analysis, but they do not remove the
lazy exact collision guard from cache construction or route certification.
Large chunked maps therefore retain the same collision authority without
requiring per-map environment overrides.
For each selected V12 route, cache construction runs a bounded, goal-directed
mesh-roadmap search inside that route's horizontal footprint corridor. Without
an explicit sidecar, OBJ declaration-order vertex zero is the route anchor.
It is only a locator: it must bind within the fixed snap cap to an
occupied-wins free attachment in the selected surface-gap layer. Only the
certified attachment is executable; a spatial chunk sort cannot replace this
source provenance. The primary lattice is fixed
at 1 m X/Z by 0.25 m Y; only the bounded exact retry described above may use
0.5 m X/Z while retaining 0.25 m Y. Local moves use its immediate
neighbors, while longer route-guided moves provide bounded shortcuts. Every
candidate edge samples intermediate voxel evidence at half-lattice spacing,
including every crossed lattice boundary and every interval between crossings,
and then passes an exact cached-mesh segment check. Certificate simulations
reuse that partition-invariant sampling rule so checkpoint subdivision cannot
expose a voxel skipped by preflight. Intermediate route points remain horizontal heuristics, never terminals, and
the bounded endpoint candidates may select only an exact-reachable free center. If
none is reachable, no authoritative mesh graph is published. A successful
build publishes the resulting branch-free path and its first exact node as the
published route start, alongside the fixed voxel
atlas and bounded compatibility graph. The fixed chunks also replace the old
separate fine-tile layer for local orthogonal evidence. The cache persists the
bounded startup-ingress radius. An inferred or authored start connector still
requires voxel and exact-mesh proof.
Map load does not resolve or apply a certified mesh-graph entrance. Ordinary
viewing starts at the first manifest chunk center; an explicit certificate run
supplies its own start position and validates it without changing viewer state.
Navigation cache certification is deliberately split into independent phases.
The `artifacts` phase validates the manifest, render chunks, navigation sidecar
paths, navigation-chunk counts, inferred/authored entrance binding, and the
complete source-hint span without deserializing the large graph. A safe
midpoint-to-end route therefore fails before graph loading. The
`graph` phase loads and validates the authoritative prepared graph (the V12
exact mesh path for current caches) and terminal-route coverage profile. The `route` phase
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

- `caveviewer.core.hardware.system_memory`: typed total/current system-RAM
  availability probes and legacy total-RAM fallback.
- `caveviewer.core.hardware.gpu_memory`: typed active-GPU memory probes and
  conservative fallback budgets.
- `caveviewer.core.hardware.memory_targets`: RAM and GPU utilization target
  parsing.
- `caveviewer.core.workers.allocation`: CPU caps and shared worker RAM admission.
- `caveviewer.core.streaming.budget`: pure typed-memory-to-residency policy,
  chunk-size estimation, and residency limits.
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

Streaming memory probes are converted into immutable capability facts before
the pure residency policy runs. Measured RAM and GPU budgets use their selected
targets; unknown inputs use a deterministic 1 GB fallback envelope, and unknown
RAM cannot raise the normal conservative utilization target. This keeps a probe
failure from becoming an unbounded residency allowance while preserving a
minimal streaming path.

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

### Capability, policy, and feature-gating contract

Platform-dependent feature work follows one direction:

```text
edge probe -> immutable CapabilityResult -> pure policy -> FeatureDecision
          -> injected adapter or service -> feature execution
```

Probes report facts and diagnostics-safe evidence; they do not choose product
behavior. Policies receive only those facts and return an immutable decision
with a stable `reason_code`, concise user-safe `explanation`, and selected
`route`. Adapters and `DesktopServices` perform the chosen native action but
do not decide whether the product feature is available.

`PlatformRuntime.feature_gates` contains only process-stable decisions, such
as automatic-update compatibility and the selected native route for revealing
a verified update package. It is composed once after command-line overrides,
then injected into every interactive viewer path, including a direct CLI map
launch. A mutable action prerequisite, such as an ffmpeg path or a writable
recording folder, uses an on-demand preflight instead of a cached startup gate.
The preflight pairs one fresh capability result with the policy decision
derived from that same snapshot.

Verified update-package reveal uses a focused adapter. At composition it
declares `finder`, `explorer`, or Linux `desktop_service` without mounting a
DMG, launching a file manager, or contacting D-Bus. The pure policy stores the
resulting static decision, and `UpdateManager` checks it again immediately
before revealing the verified payload. The action remains non-executing:
macOS's existing read-only DMG mount/reveal path, Windows Explorer selection,
and Linux desktop-service fallback are preserved behind the focused facade.
Direct compatibility callers use a visible degraded `legacy_adapter` route
until they adopt an injected runtime.

Verified update-package storage uses a similarly focused adapter, but it is
not a feature gate. Checksum verification has already completed when
`UpdateManager` calls `UpdatePackageStorageAdapter`, while the availability of
a user-visible local destination can change at any time. The adapter promotes
the temporary verified payload and returns its final path; a storage exception
is an ordinary update-workflow failure and still runs the normal temporary-file
cleanup. The current compatibility facade delegates to the established native
adapter behavior, preserving macOS DMG naming, Windows/default Downloads
handling, and Linux AppImage permissions until those implementations move
behind the narrow contract.

Saved-recording reveal is another focused action, not a feature gate. The
encoder has already reported success when `CaveViewerWindow` calls
`SavedRecordingRevealAdapter`, and the action is only a post-save convenience
after a user-visible stop. A failure to launch Finder, Explorer, or the Linux
desktop reveal route is logged but cannot downgrade the completed recording's
success state. The compatibility facade delegates to the existing
`reveal_file()` behavior until native implementations move behind the narrow
contract.

Recording encoder startup is likewise a focused action adapter, not another
recording gate. After the existing on-demand preflight has confirmed ffmpeg and
the output directory, `RecordingProcessAdapter` supplies only the native
non-command `Popen` options immediately before the encoder session starts. It
does not select an ffmpeg binary, build the command, or alter recording policy.
The current compatibility facade preserves Windows console suppression through
`STARTUPINFO` and `CREATE_NO_WINDOW`, while default, macOS, and Linux behavior
remains unchanged.

TLS trust augmentation is also a focused action adapter, not a capability gate.
Each update-network request creates Python's normal verifying SSL context, then
asks `TlsTrustAdapter` to add any native trust roots before it contacts a
manifest, signature, or verified payload URL. The compatibility facade
preserves Windows `CA`/`ROOT` certificate-store augmentation and the empty
default, macOS, and Linux behavior without disabling certificate verification.
The process-global `truststore` startup compatibility path remains separate;
this adapter does not change process initialization or network policy.

Directory selection follows the same on-demand contract. Its immutable target
declares an executable route rather than performing a desktop request:
Linux declares `portal_then_tk`, portable desktop services declare `tk`, and
legacy injected services use the conservative `injected` route. The declaration
does not create Tk resources or contact D-Bus. Map-opening and Preferences
browse actions obtain a fresh preflight immediately before invoking the chooser;
the Portal service still owns the action-time fallback to Tk if its current
request fails. The Preferences “Downloaded maps folder” control therefore
shares the same on-demand contract used by Map Library storage without adding a
separate startup gate.

Feature-state semantics are fixed:

| State | Presentation | Execution |
| --- | --- | --- |
| `enabled` | Normal feature affordance | The selected normal route may run. |
| `degraded` | Available with its fallback explained | The selected safe fallback route may run. |
| `disabled` | May show a concise explanation | No route may run. |
| `hidden` | Do not present the feature | No route may run. |

`UNKNOWN` is a capability fact, not a feature state. Each policy explicitly
chooses whether it can use a conservative fallback or must fail closed. A UI
button state is never the enforcement boundary: services re-evaluate mutable
preconditions immediately before irreversible work. Development overrides may
disable behavior for testing but never bypass a hard safety or compatibility
requirement.

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
The Map Library also owns the Guided Dive action-time handoff: it invokes the
desktop file-selection service only after the map-local discovery policy is
enabled, runs the selected trace/cache preflight, and leaves splash only after
the resulting target is executable. It does not reuse the directory-selection
gate for this file-picker action.

Directory selection, file reveal, notifications, and idle/suspend inhibition
use the separate `DesktopServices` capability. Linux asks XDG Desktop Portal
first and falls back to Tk or `xdg-open` only when the portal is unavailable.
Map-folder selection is policy-gated independently of the other desktop
actions: an enabled Portal/Tk composite or degraded Tk/injected route may run,
while a missing or indeterminate chooser route is blocked before the chooser is
created. Long map library downloads request desktop notification and inhibit
support through this same capability, but the visible Map Library dialog suppresses
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
