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

## Cache and Guided Dive coverage

Normal `caveviewer-chunker` and GUI cache builds publish render assets, render
chunks, the manifest, and the Guided Dive identity. They do not create navigation
metadata, voxel graphs, or certificate artifacts. Tests must prove standard and
incremental builds write the same portable identity that trace playback checks.

Historical `_cache/navigation_certificate/` directories are outside the render
cache contract. Normal cache validation and map opening must ignore them without
reading, migrating, or changing their contents.

Manual route-trace coverage must exercise `Cmd/Ctrl+T` countdown/start/stop,
post-movement pose sampling, orientation and stationary-heartbeat thresholds,
bounded-queue drop reporting, final-pose retention, background write failure,
map-switch cleanup, saved-trace confirmation and native reveal only after atomic
publication and its visible confirmation duration, and bookmark/minimap
discontinuities. Test output belongs under `tmp_path`; generated `_guided_dives`
directories are never repository fixtures.

Recorded Dive coverage must validate bounded JSONL parsing, its versioned
source/cache identity contract, exact first and final poses, frame-rate-independent
interpolation, instantaneous declared discontinuities, orientation-only paused
inspection, pose restoration and chunk buffering on resume, unchanged trace files,
and direct camera application that bypasses the manual navigation guard. Map Library
coverage must also prove that no trace hides the Guided Dive action and that a
selected trace is preflighted against its map-local source and current cache before
the splash session can launch it.

Cache-rebuild coverage must prove row eligibility and disabled explanations,
action-time revalidation, per-cache build ownership, child progress and OBJ pause
behavior, preservation of the prior cache on failure, and background-only terminal
notifications.

## Release gates

An individually dispatched platform release workflow calls the Essential Tests
workflow before its package job. `All Platform Release` calls Essential Tests
once, then invokes every platform workflow with its duplicate internal gate
disabled. A failed or canceled shared gate prevents every package job from
starting. After a successful gate, all package jobs may run concurrently; the
release finalizer runs only after every requested package succeeds.

Release dispatches may set `reuse_pr_validation` only when the selected source
has already passed PR validation and no application, packaging, dependency,
test, or workflow change has occurred since. It omits duplicate source suites
while retaining every platform build and release-time package check. A changelog
or other release-only metadata edit does not make the source suite material
again.

For pull requests, Essential Tests classifies the diff before starting the
source suites. Changelog and documentation edits, plus generated release
metadata (the version assignment, one AppStream release entry, and signed
update manifests), receive a lightweight validation instead of the full matrix.
That validation checks the allowed diff shape, metadata syntax and consistency,
and every update-manifest signature. Any other changed path—or an indeterminate
classification—runs the full suite. The existing required check jobs still run
as lightweight successful jobs for the metadata case, so protected `main`
remains mergeable without weakening its PR rule. Do not replace this with a
workflow-level path filter: GitHub leaves required checks pending when an
entire workflow is skipped. Malformed release metadata fails the lightweight
validation and cannot silently bypass the source suite.

The single release finalizer commits all requested manifests back to the
selected branch. Branch CI deliberately ignores that metadata-only commit,
which avoids rerunning broad tests and package smokes after a release. Normal
code and package changes still start their relevant branch CI. See
[releases.md](releases.md) for the complete sequence.

The normal macOS matrix runs on ARM64. Intel-specific coverage is provided by
`macos-x86_64-package-smoke.yml`, which runs the complete suite and CLI smoke
checks natively on `macos-15-intel`, builds the x86_64 DMG, and validates the
mounted package. The manually dispatched Intel release workflow repeats those
native checks unless it is explicitly reusing already-successful PR validation.

## Before handoff

1. Run the most focused relevant tests.
2. Run the complete suite when the environment supports it.
3. Run `git diff --check`.
4. Inspect `git status` for generated or unrelated files.
5. Report platform-specific tests that were not available locally.
