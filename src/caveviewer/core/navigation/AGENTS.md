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
canonical broader component boundaries remain in
`docs/development/architecture.md`; the local Guided Dive handoff and current
preflight design are in `docs/development/.agents/auto-dive-context.md` and
`docs/development/.agents/easiest-terminal-fixed-route.md`.

## Architecture outline

The navigation package is a core-only policy and data layer:

```text
cache builder/import
        |
        v
versioned navigation sidecar
        |
        v
NavigationVoxelAtlas + prepared V10 mesh graph
        |
        +--> bounded local route selection/recovery
        +--> startup preflight global reachability
        +--> graph node/edge + voxel/mesh safety validation
        |
        v
immutable route/preflight result
        |
        v
GUI controller owns activation, streaming, camera poses, and workers
```

The prepared V10 mesh-derived graph is the production route authority. Graph
native plans carry the atlas and graph directly; they do not load a centerline
or consult centerline clearance scores. The coarse true-3D voxel graph and
chunked atlas remain occupancy, clearance, coverage, and compatibility
evidence. The centerline remains a compatibility planner and a cache-generation
hint only. Production navigation must not silently fall back to either one
when the mesh graph is missing, stale, unsafe, or unable to provide a route.

The cache and graph layers own topology, components, line-of-sight edges,
clearance/volume metrics, terminal labels, unknown-boundary labels, and bounded
storage. Runtime core navigation may use cached chunk/mesh interfaces, but it
must not rebuild the whole map mesh or perform unbounded global rasterization
on the render thread.

The exact collision seam remains authoritative after graph selection: graph
node/edge clearance, voxel occupancy and clearance samples, segment sampling,
and the cached chunk mesh guard must be preserved for every executable route.
A graph edge is evidence, not permission to move by itself. The
camera-to-start connector is validated as an explicit segment rather than
teleporting to the nearest graph node. Production graph-native planning fails
closed before publishing a route when the cached mesh guard is unavailable;
the lower-level validator may remain mesh-optional only for isolated graph or
voxel fixtures that cannot execute a camera route.

## Guided Dive lifecycle contract

Startup must follow this order:

```text
request
  -> graph-wide preflight in a bounded startup worker
  -> READY: activate the exact validated route
  -> continuous local scan/recovery while active
```

`INDETERMINATE` and `FAILED` preflight results must not activate the camera
controller. `INDETERMINATE` is appropriate when no graph terminal/frontier can
be selected or an exact safety check is unavailable. Under the default
`easiest_terminal` goal, an incomplete mesh-safe prefix is not a valid startup
route; preflight must find a known terminal or fail closed. An
`unknown_boundary` label selected by the explicit farthest/frontier policy is
expected evidence and does not claim that the cave ends there; diagnostics must
still report incomplete cache coverage.

`build_auto_dive_preflight_plan` returns the explicit preflight result. The
longest metadata route is used only to identify the route-specific prepared
cache; it does not define the terminal and does not load a centerline
descriptor. `build_voxel_graph_auto_dive_plan` is the production runtime
planner and is also used by continuous scans.
Preflight snaps the camera to the starting graph component, enumerates that
component's known `terminal` nodes and `unknown_boundary` frontiers, and applies
the configured route goal. The default `easiest_terminal` goal excludes
unknown boundaries and selects the shortest reachable known terminal, using
clearance and available volume as deterministic comfort tie-breaks. It then
uses the same shortest-physical prepared-graph path to that terminal. The
explicit `farthest_terminal` goal retains the historical heading-aware
longest-passage and frontier behavior. The selected graph node's center is the
terminal target. Preflight prefetches route chunks and validates the executable
points with graph node/edge clearance, voxel probes, the camera connector, and
the cached-mesh seam. A `READY` result
carries the same immutable `AutoDivePlan` that the GUI activates. Startup
cancellation may cancel a queued worker or discard a completed result after the
map changes; both graph searches remain bounded by prepared graph state and
expansion limits.

If an edge on the easiest graph-terminal route is blocked by the cached mesh,
preflight keeps that known terminal and attempts a bounded spine repair: a
persisted 1 m fine tile is aggregated to a local 2 m x/z, 1 m y graph, with a
native 1 m fallback. An exact-safe local path may rejoin only a later node of
the already selected physical graph spine; it must not globally replan or enter
another branch. The immutable published ledger contains both the prepared and
refined segments. If the complete ledger cannot be proven, easiest mode returns
a non-READY result; it never publishes an incomplete prefix as a terminal
route. The explicit
farthest-terminal policy may instead select the farthest exact-safe frontier.
That route is still `READY` only after exact validation, but it is marked
`coverage_incomplete`, `replan_at_end`, and not `terminal_reached`; the
controller must continue with its continuous scan at that frontier. A
mesh-safe frontier is a temporary safe boundary, never a replacement terminal
claim and never permission to ignore the blocked edge. If no terminal or
unknown-boundary candidate is reachable but a safe graph prefix exists,
farthest mode may publish the farthest such prefix under the same
incomplete/replan contract; this is a bounded handoff point, not an endpoint.

The initial plan returned by preflight must be the route that is executed. Do
not preflight one path and then call an independent local planner that can
replace it before activation.

Initial graph camera placement uses an explicit `navigation_start` when the
cache provides one. For older manifests without that sidecar, the first point
of the selected route metadata is only a startup-position hint; it is not a
terminal, route authority, or safety descriptor. The GUI's coarse chunk
navigation guard may continue to constrain ordinary manual movement, but it
must not rewrite a graph-native startup pose or an active graph-native route.
Those positions are validated by the graph/voxel/mesh safety seam instead.

Continuous scanning is speculative runtime adaptation. It may run one full
forward-hemisphere scan at a time and hand results to its owner, but every
result must pass source-sequence, start-distance, current-forward-progress,
and collision checks before acceptance. A late result is discarded. The
controller must hold the last safe frontier while a replacement is pending.
When a request is made from a moving camera and the handoff window is short,
the owner pins the route at the request pose so a valid result remains
attachable; stale results may be re-anchored from the actual camera only a
bounded number of times.

A mesh-safe scan is still speculative. If it is materially shorter than the
remaining validated route, retain the validated prefix and defer another scan
until the two horizons are comparable; record the result as
`shorter_than_validated_prefix`. When local mesh validation finds a collision,
trim at the first failed segment and reuse the existing ranked local voxel
route while rebuilding the smaller graph. Do not rerun the bounded voxel
search for every mesh retry, and bound near-duplicate handoffs by the local
route/voxel scale instead of using coarse graph cadence as a fixed minimum
executable step.

Every scan has a cooperative deadline. The owner computes the remaining time
to the current safe frontier, reserves 1.0 seconds for handoff and exact
validation, and caps the scan budget at 6.0 seconds. A full scan starts only
when at least 7.0 seconds remain at the safe frontier (the 6.0-second budget
plus the handoff reserve); a budget below 0.75 seconds is not useful for a
full scan. Otherwise the owner keeps the validated route and uses the single
authoritative fallback. The worker passes the remaining budget into the core
graph/local-voxel planner; graph expansion, local mesh sampling, voxel search,
and route publication check it cooperatively. A deadline miss is typed
`DEADLINE_EXCEEDED`, produces no partial route, and is recorded with the scan
generation and source plan.

After a deadline miss, continue only on the already validated route until its
safe frontier, then hold the camera and attempt exactly one bounded,
authoritative prepared-graph replan with exact graph/voxel/mesh validation.
If that fallback cannot produce a fresh safe route within the normal replan
handoff budget, enter user assistance. Do not restart the same continuous
scan indefinitely and do not execute a route returned after its deadline.

If a scan reaches an `unknown_boundary` with no bounded frontier or onward
exit, classify the result as `FRONTIER_EXHAUSTED` rather than treating a short
route as progress. The initial frontier-ended result is held as diagnostic
evidence and is not activated. The owner may request one bounded local
expansion from the cached mesh. That expansion must be converted into a temporary
`NavigationVoxel3DGraph`, retain `coverage_incomplete`, and pass the same
graph-node, graph-edge, voxel, connector, and cached-mesh safety validator
before it can be executed. A stable `(cache/graph snapshot, start key, target
key, route prefix)` identity is used to detect no-progress; once the bounded
expansion is also rejected at the same camera position, stop requesting the
same scan and enter user assistance. This is a lifecycle guard, not a
terminal-node claim.

If mesh evidence disappears after startup, the runtime graph planner raises a
typed `mesh_collision_guard_unavailable` authority failure. The continuous
worker records that reason; after its bounded failure allowance the GUI holds
the last validated route and enters user assistance. It must not accept a
graph/voxel-only continuation.

Probe roll is not camera roll. Recovery may use roll to inspect virtual probe
orientations, but that value must not be copied into executed route keyframes.
Normal navigation routes use zero roll unless an explicit, separately tested
camera-roll feature is introduced.

## Terminal goal rule

Preflight determines the terminal node from the prepared V10 mesh graph:

1. Use the longest metadata route only to select the route-specific prepared
   voxel cache. Do not use its endpoint or centerline clearance as the
   navigation goal or a safety gate.
2. Snap the camera to the nearest valid starting graph node within the
   prepared graph's explicit start tolerance.
3. Apply `settings.route_goal`. For `easiest_terminal`, consider only known
   nodes marked `terminal`; for `farthest_terminal`, also consider prepared
   `unknown_boundary` frontiers. A known `terminal` is a topological end of
   prepared free space; an `unknown_boundary` is a prepared evidence frontier
   and is not proof that the cave ends there.
4. Run bounded Dijkstra over valid directed graph edges using accumulated edge
   distance. The easiest policy selects the shortest reachable known terminal,
   with clearance, available-volume, and key tie-breaks, then executes the
   same shortest-physical path. The farthest policy selects the longest
   reachable terminal/frontier with deterministic topology and key tie-breaks.
   Do not use Cartesian distance, `progress_m`, or a centerline endpoint.
5. Use the selected graph node's center as the terminal target and run exact
   collision validation to that same node. If a coarse edge is mesh-blocked,
   easiest mode may only repair it through a bounded persisted-fine-tile bridge
   that rejoins a later node of the fixed spine. Farthest mode may use a safe
   frontier or prefix, but must not call it a terminal.

The centerline is not the production route, terminal authority, or graph-mode
safety descriptor. If the selected node or executable path contains
`unknown_boundary`, preserve the goal and record `coverage_incomplete`; do not
claim that the cave has no geometry beyond that endpoint. If the starting
component has no reachable terminal/frontier candidate, return `INDETERMINATE`
rather than falling back to centerline geometry. Do not hardcode a cave ID,
route ID, endpoint coordinate, or route length; use the loaded graph and
metadata for every cave.

## Required architecture validation after every code change

Do not finish a navigation change after tests alone. After each code change,
perform an explicit architecture validation and include this table in the
handoff or review note:

| Condition | Status (`full`/`partial`/`none`) | Evidence |
| --- | --- | --- |
| Core has no GUI/Tk/OpenGL dependency |  | imports and core tests |
| Prepared V10 mesh graph remains route authority |  | route source and authority diagnostics |
| Exact graph/voxel/mesh safety remains in the path |  | graph safety tests and failure case |
| Startup preflight gates controller activation |  | ready/indeterminate/failed tests |
| Route-goal policy defines the preflight goal |  | easiest/farthest selection, endpoint-snap, and wrong-component tests |
| Worker ownership is bounded by cooperative deadlines, cancellable, and nonblocking |  | lifecycle/timeout/stale-result tests |
| Continuous scanning remains speculative, deadline-bounded, and freshness-checked |  | sequence/frontier/timeout/rejection tests |
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
