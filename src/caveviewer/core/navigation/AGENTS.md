# Navigation instructions

Applies to: `src/caveviewer/core/navigation/`
Inherits: `/AGENTS.md`, `/src/AGENTS.md`, `/src/caveviewer/core/AGENTS.md`
Overrides: none
Validation:

```bash
.venv-dev/bin/python -m pytest -p no:cacheprovider -q tests/unit/core/test_navigation_*.py
.venv-dev/bin/python -m pytest -p no:cacheprovider -q tests/unit/core
git diff --check
```

These instructions define the navigation-specific architecture contract. The
canonical broader component boundaries and the current offline
navigation-certificate contract remain in `docs/development/architecture.md`. Files below
`docs/development/.agents/` are historical design notes, not current
production authority.

## Architecture outline

The navigation package is a core-only policy and data layer:

```text
cache builder/import
        |
        v
versioned navigation sidecar
        |
        v
NavigationVoxelAtlas + prepared V12 exact mesh path
        |
        +--> complete longest-route selection
        +--> startup preflight reachability
        +--> graph node/edge + voxel/mesh safety validation
        |
        v
immutable route/preflight result
        |
        v
offline certificate and verification tooling consumes the result
        |
        v
viewer remains manual (or uses a Recorded Dive trace)
```

The prepared V12 mesh-derived path is the certificate route authority. Graph
native plans carry the atlas and graph directly; they do not load a centerline
or consult centerline clearance scores. The coarse true-3D voxel graph and
chunked atlas remain occupancy, clearance, coverage, and compatibility
evidence. The centerline remains a compatibility planner and a cache-generation
X/Z ordering hint only. Certificate operations must not silently fall back to
either one when the mesh graph is missing, stale, unsafe, or unable to provide
a route.

V12 voxel evidence is fixed and orthogonal: X/Z are no coarser than 1 m and Y
is no coarser than 0.25 m. Capacity
pressure subdivides chunks and surface-sampling truncation fails closed.
Overlapping chunks use occupied-wins surface semantics. Normally, one packed
six-connected component joining bounded ingress evidence to the selected known
terminal feeds the exact path build. Merge all bounded corridor-filtered
non-surface cells before component selection; a local seed must not erase seam
evidence. Disconnected surface-gap-seeded fragments are never combined as a
certificate route. If the source-connected fixed component does not reach the
real final endpoint, the route fails closed before exact-roadmap publication.
Extract the certificate path through ordered surface-gap waypoint candidates
with six-connected searches and one cumulative expansion ledger. Check every
candidate edge against partition-invariant voxel samples and the exact cached
mesh before retaining it; block a rejection and reroute only that bounded leg.
A route-cell candidate must be a selected-component free-voxel center in that
exact footprint cell. Entrance and final candidates must be inside the one
source-connected bounded vertical interval selected for their cells, with at
most half a vertical voxel of Y quantization tolerance. An intermediate cell
may use a free key inside the hull of one specific continuity-compatible
adjacent interval pair when its selected interval has no lattice key. Never
merge every stacked interval in neighboring cells into a broad Y slab.
Midpoints rank proposals but are not required executable positions.
Before ranking a terminal component, prove it has at least one such candidate
for every ordered route cell; cap candidates only within that selected
component, preserving interval diversity. The inferred start locator may
attach only to the first group, and terminal candidates come only from the
final group--a local neighbor shell must not reintroduce another layer.
A smoothing shortcut is executable only after those same checks. Intermediate
waypoints are never terminals, and capacity exhaustion must publish neither a
prefix nor a shorter competing recommendation while the longer route remains
unresolved.

A valid explicit entrance sidecar overrides automatic inference. Otherwise an
OBJ uses declaration-order vertex zero as its immutable entrance anchor. The
surface vertex itself is non-executable and requires a bounded attachment to a
free voxel in the selected surface-gap layer; only that certified interior
attachment becomes executable. Never replace this source-order provenance with
a numeric or lexical manifest-chunk sort. The selected route must begin at source hint
zero and end at the final source hint; never retry or publish a suffix. The
exact persisted path's first node is the published certificate route start. V11
and older supported sidecars remain readable, but only V12 may authorize
certificate workflows.

OBJ-derived centerline Y values are hints, not topology: use the polyline only
for horizontal ordering and a bounded X/Z envelope. The 0.25 m vertical field
and exact mesh connectivity choose the cave layer. Persist every bounded
surface-gap seed using sparse 0.25 m-or-finer vertical bins; never substitute
route Y for missing surface evidence. If exact traversal fails on
the 1 m waypoint lattice, one bounded 0.5 m X/Z by 0.25 m Y retry starts in a
4 m horizontal envelope over the same admitted evidence and may widen once to
8 m after exhaustive non-capacity failure. It must not create free evidence:
retain point membership, crossed-boundary sampling, and exact cached-mesh
checks on every edge. It must traverse the same ordered surface-gap gates,
consume only the coarse attempt's remaining node ledger, and reject retained
key revisits. The legacy raw-guide adaptive builder is not a V12 publication
fallback.
For a steep surface-gap transition, widen both non-entrance cells touched by
the specific selected interval pair and available cardinal support cells;
keep the entrance interval immutable. This merely proposes evidence and must
not join components or authorize motion.
It is safe to skip voxel/mesh collision work for a relaxation that cannot
improve an existing route cost, because that existing cost was admitted only
through an already-safe edge. Never apply that shortcut to an improving edge.
When a long route exceeds the packed-key budget, narrow its horizontal
envelope deterministically, but never below the X/Z uncertainty of an OBJ
footprint cell. Never solve capacity by coarsening cubes, using a per-map
override, or weakening exact edge checks.
The envelope deliberately retains vertical candidates. A raw endpoint may snap
only to a bounded free cube inside it. Up to 64 bounded free endpoint centers
may be exact-tested as terminal candidates; persist only the
candidate actually reached by the mesh-safe path. Never accept an ingress seed
as a zero-edge terminal. The chosen center must pass the same runtime
certification as every other terminal.
Legacy metadata may still contain interpolated route Y values for compatibility
rendering and diagnostics. Strict OBJ V12 construction must not use them to
choose, fill, or filter a cave layer. Missing surface-gap evidence fails closed;
it is never inherited from a nearby route sample.
Selected packed evidence may provide the already-admitted minimum clearance
for cache-time A* probes. This optimization must not replace exact mesh checks
on edges or persisted voxel-chunk probes during certification and runtime.
Cached render-chunk bounds may be indexed into a fixed spatial bucket map so
an edge checks only nearby chunks. Keep the final exact chunk-AABB and triangle
intersection tests authoritative, include boundary-touching chunks, and fall
back to the full chunk list for malformed or pathologically large queries.
Voxel rasterization may vectorize the per-mesh local-AABB prefilter, but it
must preserve triangle order, bounded surface sampling, occupied-cell results,
and fail-closed truncation behavior.

The cache and graph layers own topology, components, line-of-sight edges,
clearance/volume metrics, terminal labels, unknown-boundary labels, and bounded
storage. Runtime core navigation may use cached chunk/mesh interfaces, but it
must not rebuild the whole map mesh or perform unbounded global rasterization
on the render thread.

The exact collision seam remains authoritative after graph selection: graph
node/edge clearance, voxel occupancy and clearance samples, segment sampling,
and the cached chunk mesh guard must be preserved for every executable route.
A cache-time and runtime edge probe must include every crossed voxel-lattice
boundary plus an interior sample between consecutive crossings, not only
evenly spaced samples. This keeps the safety result invariant when runtime
splits a preflight segment into checkpoints.
A graph edge is evidence, not permission to move by itself. The
camera-to-start connector is validated as an explicit segment rather than
teleporting to the nearest graph node. Production graph-native planning fails
closed before publishing a route when the cached mesh guard is unavailable;
the lower-level validator may remain mesh-optional only for isolated graph or
voxel fixtures that cannot execute a camera route.

## Offline navigation certificate contract

The viewer no longer has an autonomous camera controller. Cmd/Ctrl+A is
unhandled, ordinary map opening uses the render-cache start position, and no
module in `caveviewer.gui` may import or execute an auto-dive planner. Preserve
`Cmd/Ctrl+T` manual tracing and Recorded Dive separately; neither is a navigation
certificate consumer.

`autodive.py` and its `AutoDive*` names remain a compatibility-oriented core
implementation detail for offline certificate construction, verification, and
core tests. `certificate.py` may call `build_auto_dive_preflight_plan` and
`build_voxel_graph_auto_dive_plan` to validate a cached route. Their results
are non-executable diagnostics: they must never create a camera worker, alter
`StreamingWorld`, change the normal viewer start position, or write to the
render cache.

Cache construction chooses and certifies the longest complete exact-safe
non-circular route from its requested entrance. The graph/voxel/mesh safety
seam, bounded storage, exact edge checks, route goals, and compatibility rules
remain authoritative for the certificate. A missing or stale optional sidecar
may make a certificate unresolved, but it must never prevent ordinary render
cache viewing or trigger a viewer-side cache rebuild.

When changing this area, do not reintroduce a GUI runtime through a callback or
background worker. Keep any recovery, prefetch, or simulation work behind the
offline certificate boundary, bounded and read-only with respect to the
published cache.

## Required architecture validation after every code change

Do not finish a navigation change after tests alone. After each code change,
perform an explicit architecture validation and include this table in the
handoff or review note:

| Condition | Status (`full`/`partial`/`none`) | Evidence |
| --- | --- | --- |
| Core has no GUI/Tk/OpenGL dependency |  | imports and core tests |
| Prepared V12 exact mesh path remains route authority |  | route source and authority diagnostics |
| Exact graph/voxel/mesh safety remains in the path |  | graph safety tests and failure case |
| Viewer has no autonomous controller or planner import |  | GUI import/shortcut tests |
| Route-goal policy defines the offline certificate goal |  | longest/easiest/farthest selection and wrong-component tests |
| Certificate analysis stays bounded and read-only |  | certificate timeout and failure tests |
| No certificate result can activate or steer a viewer camera |  | GUI regression tests |
| Executed camera roll is independent of probe roll |  | route keyframe roll assertions |
| Cache compatibility and rebuild behavior are preserved |  | cache version/load tests |

Use `partial` when the repository does not yet implement the condition, when
the current cache cannot prove it, or when a safety seam is optional. Explain
what remains missing. Do not mark a condition `full` merely because a planner
returned a route.

The minimum validation sequence for a behavioral change is:

1. Inspect `git status` and preserve unrelated worktree changes.
2. Add focused core regression tests, including failure and cancellation paths.
3. Run the navigation tests, then `tests/unit/core` when practical.
4. Inspect diagnostics for route authority, collision outcome, freshness, and
   worker ownership.
5. Complete the architecture table above, run `git diff --check`, and report
   any `partial`/`none` condition before handoff.

Do not import GUI classes into this package to make a test or callback easier.
Inject pure callbacks, immutable values, or small interfaces at the boundary.
