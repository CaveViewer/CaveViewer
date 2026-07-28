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
preflight design are in `docs/development/.agents/auto-dive-context.md`.

## Architecture outline

The navigation package is a core-only policy and data layer:

```text
cache builder/import
        |
        v
versioned navigation sidecar
        |
        v
NavigationVoxelAtlas + prepared true-3D graph
        |
        +--> bounded local route selection/recovery
        +--> startup preflight global reachability (planned seam)
        +--> cached voxel/Y-range/mesh collision validation
        |
        v
immutable route/preflight result
        |
        v
GUI controller owns activation, streaming, camera poses, and workers
```

The prepared true-3D voxel graph is the production route authority. The
centerline is allowed to provide cache-generation seeds and footprint/Y-range
geometry, but production navigation must not silently fall back to it when the
prepared graph is missing, stale, unsafe, or unable to provide a route.

The cache and graph layers own topology, components, line-of-sight edges,
clearance/volume metrics, terminal labels, unknown-boundary labels, and bounded
storage. Runtime core navigation may use cached chunk/mesh interfaces, but it
must not rebuild the whole map mesh or perform unbounded global rasterization
on the render thread.

The exact collision seam remains authoritative after graph selection:
voxel occupancy, Y ranges, lateral clearance, segment sampling, and the cached
chunk mesh guard must be preserved for every executable route. A graph edge or
centerline connection is evidence, not permission to move by itself.

## Guided Dive lifecycle contract

Startup must follow this order:

```text
request
  -> preflight in a bounded/cancellable worker
  -> READY: activate the exact validated route
  -> continuous local scan/recovery while active
```

`INDETERMINATE` and `FAILED` preflight results must not activate the camera
controller. `INDETERMINATE` is appropriate when no terminal anchor can be
defined or an exact safety check is unavailable. An `unknown_boundary` label at
the intentionally selected longest-passage endpoint is expected evidence and
does not override that goal; diagnostics must still report incomplete cache
coverage.

The initial plan returned by preflight must be the route that is executed. Do
not preflight one path and then call an independent local planner that can
replace it before activation.

Continuous scanning is speculative runtime adaptation. It may run one full
forward-hemisphere scan at a time and hand results to its owner, but every
result must pass source-sequence, start-distance, current-forward-progress,
and collision checks before acceptance. A late result is discarded. The
controller must hold the last safe frontier while a replacement is pending.

Probe roll is not camera roll. Recovery may use roll to inspect virtual probe
orientations, but that value must not be copied into executed route keyframes.
Normal navigation routes use zero roll unless an explicit, separately tested
camera-roll feature is introduced.

## Terminal goal rule

Preflight determines the terminal point from the longest passage represented by
the currently loaded cave's navigation metadata:

1. Select the cached centerline route with the greatest `length_m`.
2. Use `navigation_start` as the entrance reference when present; otherwise
   use the current starting camera position and travel heading to orient the
   passage endpoints.
3. Treat the opposite endpoint as the terminal world-space target.
4. Snap that target to the nearest routable true-3D graph node in the starting
   node's graph component, with an explicit distance tolerance.

This does not make centerline geometry the runtime route source. The centerline
only supplies the goal anchor; the prepared true-3D graph supplies the path
and the voxel/Y-range/cached-mesh seams validate movement. A graph-local
`terminal` label is supplemental evidence, not the source of the terminal
definition. If the selected goal node is marked `unknown_boundary`, preserve
the goal, record `coverage_incomplete`, and do not claim that the cave has no
geometry beyond that endpoint. Do not hardcode a cave ID, route ID, endpoint
coordinate, or route length; use the loaded metadata for every cave.

## Required architecture validation after every code change

Do not finish a navigation change after tests alone. After each code change,
perform an explicit architecture validation and include this table in the
handoff or review note:

| Condition | Status (`full`/`partial`/`none`) | Evidence |
| --- | --- | --- |
| Core has no GUI/Tk/OpenGL dependency |  | imports and core tests |
| Prepared true-3D graph remains route authority |  | route source and authority diagnostics |
| Exact voxel/Y-range/mesh safety remains in the path |  | collision tests and failure case |
| Startup preflight gates controller activation |  | ready/indeterminate/failed tests |
| Longest-passage endpoint defines the preflight goal |  | route-selection, endpoint-snap, and wrong-component tests |
| Worker ownership is bounded, cancellable, and nonblocking |  | lifecycle/stale-result tests |
| Continuous scanning remains speculative and freshness-checked |  | sequence/frontier/rejection tests |
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
