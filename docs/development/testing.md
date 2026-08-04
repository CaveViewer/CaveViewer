# Testing

This file is the canonical testing policy and command reference. Local
`AGENTS.md` files may add narrower requirements, but should not repeat this
file's commands, marker definitions, or coverage thresholds.

## Test layout

- `tests/unit/core/`: parsing, cache, scheduling, memory, and other core policy.
- `tests/unit/benchmarking/`: benchmark scenario, route-selection, summary, and
  comparison policy that does not require Tk or OpenGL.
- `tests/unit/gui/`: controllers, validation, dialogs, platform adapters, and
  GUI-adjacent logic that can run deterministically.
- `tests/unit/`: application-level helpers that do not fit one package.
- `tests/integration/`: multi-component, real-filesystem, or controlled network
  workflows.

Use the markers declared in `pyproject.toml`: `integration`, `gui`, `gl`,
`slow`, and `network`. A marker describes an actual runtime requirement; it is
not a substitute for isolating avoidable external dependencies.

## Commands

Run the complete suite:

```bash
.venv-dev/bin/python -m pytest -p no:cacheprovider -q
```

Run a focused area:

```bash
.venv-dev/bin/python -m pytest -p no:cacheprovider -q tests/unit/core
```

Measure branch coverage without writing the data file into a restricted or
dirty source tree:

```bash
COVERAGE_FILE=/tmp/caveviewer.coverage \
  .venv-dev/bin/python -m pytest -p no:cacheprovider \
  --cov=caveviewer.app \
  --cov=caveviewer.core.chunking.builder \
  --cov=caveviewer.core.preferences.schema \
  --cov=caveviewer.core.map.importer \
  --cov=caveviewer.core.map.source_model \
  --cov=caveviewer.core.textures.decoding \
  --cov=caveviewer.gui.preferences \
  --cov=caveviewer.gui.standard_library_maps \
  --cov=caveviewer.gui.texture_manager \
  --cov=caveviewer.gui.update_checker \
  --cov=caveviewer.gui.update_manager \
  --cov-branch
```

## Test principles

- A regression test should demonstrate the externally relevant failure and
  fail before the fix.
- Failure-path tests must assert cleanup and state consistency, not only the
  exception message.
- Use temporary directories and isolated preference/home paths.
- Do not make live network calls. Simulate responses, interruptions, short
  writes, full disks, corrupt data, and cancellation at controlled boundaries.
- Avoid timing-only synchronization. Use events, bounded waits, and explicit
  worker shutdown.
- Keep tests readable: arrange state, perform one meaningful action, and assert
  behavior and side effects.

## Coverage policy

Coverage thresholds are safety floors, not targets for writing low-value tests.
The current CI workflow enforces:

- 60% across its essential measured modules.
- 90% for `src/caveviewer/app.py`.
- 85% for `src/caveviewer/gui/standard_library_maps.py`.
- 90% for `src/caveviewer/core/chunking/builder.py`.
- 90% for `src/caveviewer/gui/update_checker.py`.

The update manager's transition, retry, cancellation, cleanup, and
platform-reveal contracts have direct unit coverage. Keep those tests
event-driven and bounded rather than depending on thread timing.

When moving modules, update CI include/source paths without lowering these
thresholds. New concurrency, cleanup, cache-format, and security-sensitive code
should receive direct tests even when the aggregate floor already passes.

## Viewer FPS benchmark

Streaming FPS regressions are measured by the manual Viewer Benchmark workflow
and the `caveviewer-benchmark` CLI. This benchmark is intentionally separate
from the always-on unit suite because it needs a real OpenGL/display stack and a
large precompiled map cache.

Use `docs/development/benchmarking.md` for setup, scenario, artifact, and
threshold policy. The workflow compares a candidate ref with a baseline ref and
uploads logs plus per-frame metrics so failures can be inspected without
re-running the viewer immediately.

For machine-local private or oversized benchmark maps, use
`caveviewer-map-benchmark`. It keeps benchmark history under the map-local
ignored `_benchmarks/` directory by default and can be installed through the
tracked local pre-push hook template for pushes to `main`.

## Navigation cache certification

Certify a generated cache in phases. The artifact phase is fast and does not
deserialize the navigation graph:

```bash
caveviewer-navigation-verify \
  --cache-dir /path/to/map/_cache \
  --source /path/to/map/source.obj \
  --phase artifacts \
  --json
```

The graph phase deliberately loads the authoritative prepared graph (the V12
exact mesh path for current caches) and checks graph geometry, coverage,
navigation chunks, and mesh-collision availability. The
artifact phase also binds an OBJ import to its declaration-order vertex-zero
anchor (or to an explicit sidecar override) and requires source hints zero
through final; collision safety alone
cannot certify a midpoint-to-end route. The
route phase additionally requires `--start X Y Z` and checks startup preflight,
exact route safety, and execution simulation. For the default fixed full-cave
route, the simulation follows the published ledger and must report zero replan
requests. Use `--profile frontier` when
incomplete cache evidence is an expected temporary boundary; use the default
`--profile full-cave` only when a known terminal and complete coverage are
required. `--phase all` runs every phase in one process and is useful for a
deep post-build report, but is not a cheap startup check for very large graphs.
The certificate's default route goal is `longest_certified_route`: it follows
the cache-selected complete non-circular route to its farthest known terminal
using the same shortest-physical prepared-graph path before exact graph, voxel,
and cached-mesh validation. The route-phase simulation follows that preflighted
route without continuous or speculative replacement. A V12 OBJ cache selects the longest
exact-certified non-circular candidate derived from declaration-order vertex
zero, unless an explicit entrance sidecar overrides it;
a longer 2D candidate that fails 3D certification must not override the
manifest recommendation. The inferred or authored locator is never a viewer
camera position; tests must prove its bounded exact-safe interior attachment is
the published certificate route start. A V12 cache
persists one compact, route-ordered path to the route's real endpoint
while retaining fixed 1 m X/Z by 0.25 m Y voxel chunks and a bounded compatibility
graph. Its bounded extractor uses surface-gap-derived waypoint candidates,
ignores imported route Y, and shares one expansion ledger across all legs.
Rejected cardinal edges reroute only within the remaining ledger, and bounded
smoothing shortcuts receive the same sampled voxel and exact mesh checks as
the original edges. Cache-time voxel samples must include every
crossed lattice boundary and every interval between crossings. Route simulation
must apply the same partition-invariant rule so a diagonal cannot pass
publication and fail only after checkpoint subdivision. The exact persisted
graph start, rather than an approximate centerline hint, is published as the
certificate route start. Every executable edge is validated by the route phase;
an intermediate hint or incomplete prefix is not
valid. Inferred/authored-start tests must require source hint zero, the final source hint,
and exactly one full-route attempt—no ranked suffix retry. Selection tests must
also keep a longer capacity-limited candidate unresolved: it must suppress a
shorter recommendation until the longer exact search either certifies or
reaches a conclusive non-capacity rejection.
When the exact 1 m X/Z waypoint lattice is disconnected only by alignment, the
builder may retry at 0.5 m X/Z while retaining 0.25 m Y in a 4 m horizontal envelope over the same fixed voxel
evidence. An exhaustive non-capacity failure may widen once to 8 m; a node-cap
failure must stop. Tests must prove the coarse failure, 4-to-8 m escalation,
the finer success, every ordered surface-gap gate, one coarse-plus-fine node
ledger, no retained-key revisit, and unchanged exact edge authority. The
legacy raw-guide adaptive path must not become a V12 publication fallback;
this retry is universal, not a per-map setting.
Search-cost regressions should prove that non-improving relaxations skip exact
collision work while every edge admitted to the shortest-path queue remains
fully validated.
OBJ footprint routes use X/Z ordering only: regressions must include wildly
incorrect intermediate and endpoint Y hints, stacked surface gaps, and prove
that the 0.25 m field plus exact mesh connectivity selects the reachable layer.
Metadata regressions must keep sparse surface bins at 0.25 m or finer and
persist every bounded gap seed; a missing surface column must never fall back
to route/interpolated Y.
Candidate regressions must bind the entrance, every intermediate gate, and the
terminal to a free-voxel center in the exact route cell. Entrance and terminal
keys must intersect their selected bounded interval with no more than half a
vertical voxel of Y tolerance. An intermediate bridge may use only the hull of
one selected adjacent interval pair. Tests must cover a midpoint that misses
the lattice while another key in the same cell/interval succeeds, reject a
nearer key in an adjacent cell or outside the interval, prove stacked layers
cannot be merged into one transition slab, enforce source/terminal snap caps,
and reject a component missing any ordered gate before it can win terminal
ranking. The certificate must also reject a prepared terminal outside the
final-cell interval evidence.
Long routes that exceed the packed graph
budget automatically retry narrower horizontal envelopes at the same voxel
resolution; diagnostics must record every attempt and selected radius. An OBJ
route must never retry below the half-diagonal X/Z uncertainty of its
footprint cells. Surface-gap transition tests must prove that steep cardinal
steps widen both sides of a specific selected interval pair, diagonal
cardinal-support cells receive a continuous sampling envelope, the entrance
gap is never widened, stacked intervals are never globally merged, and route Y
is absent from the calculation.
A noisy raw endpoint uses only vertically diverse free centers admitted by the
final footprint cell's bounded intervals. Up to 64 selected-component endpoint
centers may be considered, but the persisted terminal must be the candidate
reached by the exact mesh-safe path. Tests must cover a blocked primary with a
reachable interval-backed alternative, reject an out-of-interval neighbor
shell, and reject a zero-edge ingress as route completion.
Source-connectivity regressions must prove that disconnected surface-gap
fragments are never combined. A voxel gap or mesh-blocked bridge must publish
nothing; success requires one source-connected six-neighbour component, a
complete exact roadmap to the final endpoint, and an entrance attachment
inside the 24 m OBJ cap.
Cover a missing final endpoint voxel and a missing surface-profile column: both
must fail closed for inferred and authored starts. Legacy interpolated route Y
may remain available to compatibility callers, but tests must prove it never
fills, filters, or certifies a V12 source-connected layer.
Cache-time graph search may use the packed component's conservative minimum
clearance for constant-time candidate probes. Tests and certificates must keep
exact mesh edge checks and persisted-chunk runtime probes in the safety path.
The cached-mesh chunk index needs regressions that prove a local segment prunes
distant chunks and that a segment exactly on a bucket boundary retains every
touching chunk. These are candidate-selection tests only; exact AABB and
triangle collision checks remain required afterward.
Surface-rasterizer tests must also keep out-of-bounds triangles excluded after
the vectorized local-AABB prefilter; triangle counts and sampled surface cells
remain the behavioral contract.
Rebuild V11 or older caches before this test because they do not contain the
fixed-orthogonal V12 evidence contract. Render caches remain usable when that
optional navigation rebuild is absent.
Large-map regression fixtures must also prove that whole-map and average-chunk
triangle thresholds disable only optional speculative recovery. They must not
make the lazy exact collision guard unavailable or cause a render-only cache to
be published solely because a map exceeds those thresholds.

### Legacy isotropic cubic graph experiment

Before changing the navigation cache format, use the read-only cubic graph
experiment to measure whether existing one-metre atlas evidence supports a
complete exact-safe route:

```bash
PYTHONDONTWRITEBYTECODE=1 .venv-dev/bin/python \
  scripts/dev/navigation_cubic_graph_experiment.py \
  --cache-dir /path/to/map/_cache \
  --mode atlas \
  --voxel-size 1.0 \
  --minimum-clearance 0.25 \
  --cardinal-only \
  --json
```

Atlas mode fails rather than silently accepting source tiles coarser than the
requested isotropic resolution. To investigate one such area without changing
the cache, use `--mode region`, supply aligned `--bounds`, `--start`, and
`--target` coordinates, and let the tool revoxelize only that bounded region
from cached render-chunk triangles. The experiment stores packed free-voxel
keys, computes adjacency implicitly, rejects unsupported diagonals, and lazily
blocks exact-mesh-colliding route edges. It never publishes or overwrites cache
artifacts. A passing report is diagnostic evidence for a future cache format;
it does not make the existing cache graph authoritative.

Atlas mode may read legacy V10 tiles whose surface sampling was truncated. It
reports their count as `truncated_source_volume_count` and still requires the
final path to pass the complete cached-mesh guard. This exception exists only
to investigate old evidence; V12 never publishes truncated source volumes.

Region mode accounts for the rasterizer's extra boundary shell, excludes that
shell from graph evidence, and fails if the requested capacity would coarsen
the voxels or truncate surface sampling. `graph_voxel_capacity` is the usable
bounded region; `raster_voxel_capacity` includes the temporary shell.

Run the same exact-resolution check sequentially across every recursively
discovered `_cache` below a local map library with:

```bash
PYTHONDONTWRITEBYTECODE=1 .venv-dev/bin/python \
  scripts/dev/navigation_cubic_graph_suite.py \
  --maps-root /path/to/Maps \
  --voxel-size 1.0 \
  --json
```

This read-only suite diagnoses legacy or current atlas evidence; it does not
replace a V12 build plus the production navigation certificate. It runs maps in
separate child processes so one cave's graph and search
state are released before the next cave starts. `PASSED` means a route was
proved and exact-mesh checked. `INCOMPATIBLE_RESOLUTION` means V10 coarsened at
least one source tile, while `MISSING_ARTIFACT` means the render cache has no
usable navigation sidecar. Those outcomes are cache limitations, not geometry
failures. The command exits nonzero unless every discovered map passes.
Map labels are paths relative to `--maps-root`, so nested libraries and maps
with duplicate directory names remain unambiguous. No map data or local result
files are written or committed by the suite.

Viewer coverage must prove Cmd/Ctrl+A is unhandled and that opening a map does
not queue a navigation planner or change the ordinary cache-derived camera
position. The core certificate tests cover route preflight independently of
the GUI.
Manual route-trace coverage must exercise `Cmd/Ctrl+T` start/stop, post-movement
pose sampling, orientation and stationary-heartbeat thresholds, bounded-queue
drop reporting, final-pose retention, background write failure, map-switch
cleanup, saved-trace confirmation and native reveal only after atomic
publication, and bookmark/minimap discontinuities. Test output belongs under
`tmp_path`; generated `_guided_dives` directories are never repository
fixtures.
Recorded Dive coverage must validate bounded JSONL parsing, its versioned
source/cache identity contract, exact first and final poses,
frame-rate-independent interpolation, instantaneous declared discontinuities,
orientation-only paused inspection, pose restoration and chunk buffering on
resume, unchanged trace files, and direct camera application that bypasses the
manual navigation guard. Cache-construction coverage must prove that both
standard and incremental builds write the same portable Guided Dive identity
that trace playback checks. Map Library coverage must also prove that no trace
hides the Guided Dive action and that a selected trace is preflighted against
its map-local source and current cache before the splash session can launch it.
Use the explicit farthest/frontier profile only when testing continuation
behavior on incomplete or mesh-blocked evidence.
Asynchronous replan, pacing-hold, and continuous-scan diagnostics remain core
certificate/planner unit coverage only; they are not viewer behaviors.

## Release gates

An individually dispatched platform release workflow calls the Essential Tests
workflow before its package job. `All Platform Release` calls Essential Tests
once, then invokes every platform workflow with its duplicate internal gate
disabled. A failed or canceled shared gate prevents every package job from
starting. After a successful gate, all package jobs may run concurrently; the
release finalizer runs only after every requested package succeeds.

The single release finalizer commits all requested manifests back to the
selected branch. That push can start the normal branch CI configured for `main`
and `release/**`; such runs are separate from the single gate within the
all-platform release. See [releases.md](releases.md) for the complete sequence.

The normal macOS matrix runs on ARM64. Intel-specific coverage is provided by
`macos-x86_64-package-smoke.yml`, which runs the complete suite and CLI smoke
checks natively on `macos-15-intel`, builds the x86_64 DMG, and validates the
mounted package. The manually dispatched Intel release workflow repeats those
native checks before uploading or publishing its artifact.

## Before handoff

1. Run the most focused relevant tests.
2. Run the complete suite when the environment supports it.
3. Run `git diff --check`.
4. Inspect `git status` for generated or unrelated files.
5. Report platform-specific tests that were not available locally.
