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
  --cov=caveviewer --cov=core --cov=gui --cov-branch
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
- 85% for `gui/sample_maps.py`.
- 60% for `core/chunker.py`.
- 50% for `gui/update_checker.py`.

When moving packages, update CI include/source paths without lowering these
thresholds. New concurrency, cleanup, cache-format, and security-sensitive code
should receive direct tests even when the aggregate floor already passes.

## Before handoff

1. Run the most focused relevant tests.
2. Run the complete suite when the environment supports it.
3. Run `git diff --check`.
4. Inspect `git status` for generated or unrelated files.
5. Report platform-specific tests that were not available locally.
