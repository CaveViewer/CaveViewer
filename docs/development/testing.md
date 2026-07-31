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

## Guided Dive cache certification

Certify a generated cache in phases. The artifact phase is fast and does not
deserialize the navigation graph:

```bash
caveviewer-navigation-verify \
  --cache-dir /path/to/map/_cache \
  --source /path/to/map/source.obj \
  --phase artifacts \
  --json
```

The graph phase deliberately loads the authoritative prepared graph (the V10
mesh-derived graph for current caches) and checks graph geometry, coverage,
navigation chunks, and mesh-collision availability. The
route phase additionally requires `--start X Y Z` and checks startup preflight,
exact route safety, and execution simulation. For the default fixed full-cave
route, the simulation follows the published ledger and must report zero replan
requests. Use `--profile frontier` when
incomplete cache evidence is an expected temporary boundary; use the default
`--profile full-cave` only when a known terminal and complete coverage are
required. `--phase all` runs every phase in one process and is useful for a
deep post-build report, but is not a cheap startup check for very large graphs.
The default Guided Dive startup goal is `easiest_terminal`: it chooses the
shortest known terminal and uses the same shortest-physical prepared-graph
path before exact graph, voxel, and cached-mesh validation. It then executes
that preflighted route without continuous or speculative replacement. A V10
cache persists a compact path from an exact-mesh seeded component while
retaining the voxel atlas, coarse graph, and fine evidence. Every executable
edge and the camera ingress are exact-validated again at startup; an incomplete
prefix is not valid. Rebuild V9 or older caches before this test because they
do not contain the production mesh graph. Artifact and graph phases can still
pass while full-cave route preflight correctly fails.
Use the explicit farthest/frontier profile only when testing continuation
behavior on incomplete or mesh-blocked evidence.
During a live Guided Dive, a `replan_pacing_hold_started` event is expected
when an asynchronous continuation is requested; it records that the camera
was held at the validated request pose until the replacement route could be
checked and attached.
If a completed continuous scan is shorter than the current validated route,
expect `continuous_scan_prefix_preserved`; this is a deliberate diagnostic
rejection, not a planning failure. A local mesh hit should produce
`voxel_local_frontier_mesh_safe_prefix` without repeated
`voxel_local_frontier_route_retry` events, and the accepted handoff should be
allowed to use the fine local route scale even when coarse graph spacing is
large.
For a first interactive run, use `CAVEVIEWER_AUTO_DIVE_ACCELERATION=0` (or
the lowest Preferences acceleration) so the camera advances at the baseline
diver speed while the handoff behavior is observed.

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
