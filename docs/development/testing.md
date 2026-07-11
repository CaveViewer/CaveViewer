# Testing

## Test layout

- `tests/unit/core/`: parsing, cache, scheduling, memory, and other core policy.
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
  --cov=caveviewer.core.chunker \
  --cov=caveviewer.gui.advanced_settings \
  --cov=caveviewer.gui.sample_maps \
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
- 85% for `src/caveviewer/gui/sample_maps.py`.
- 90% for `src/caveviewer/core/chunker.py`.
- 50% for `src/caveviewer/gui/update_checker.py`.

The update manager's transition, retry, cancellation, cleanup, and
platform-reveal contracts have direct unit coverage. Keep those tests
event-driven and bounded rather than depending on thread timing.

When moving modules, update CI include/source paths without lowering these
thresholds. New concurrency, cleanup, cache-format, and security-sensitive code
should receive direct tests even when the aggregate floor already passes.

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

## Before handoff

1. Run the most focused relevant tests.
2. Run the complete suite when the environment supports it.
3. Run `git diff --check`.
4. Inspect `git status` for generated or unrelated files.
5. Report platform-specific tests that were not available locally.
