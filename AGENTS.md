# CaveViewer repository instructions

These instructions apply to the whole repository. A more specific `AGENTS.md`
inside a subdirectory supplements these rules for that area.

## Sources of truth

- `docs/development/documentation.md` defines documentation placement,
  inheritance, override, and naming rules.
- `docs/development/architecture.md` defines component boundaries and
  dependency direction.
- `docs/development/repository-layout.md` defines the current package layout and
  stable external paths. Structural migrations must be explicit, mechanical
  changes.
- `docs/development/coding-standards.md` and
  `docs/development/testing.md` define implementation and verification rules.
- `docs/development/releases.md` defines release targets, workflow sequencing,
  channels, signing, and verification.
- `docs/development/source-setup.md` remains the detailed source setup,
  packaging-variable, and runtime-configuration reference.

## Session startup

Before taking repository action, every agent must:

1. Resolve and report the repository root.
2. Read this file and every applicable scoped `AGENTS.md`.
3. Inspect the active branch and Git status without changing either.
4. Identify or create the active work document. Use ignored root `.work/` by
   default; use `docs/development/work/` only when the plan needs to be shared,
   reviewed, or retained with the implementation.
5. State the focused and complete validation commands appropriate to the work.

## Working agreement

- Before editing repository files or changing repository-related external
  state, create a work document from `docs/development/work-definition.md`.
  Store it at `.work/<work-name>.md` by default. Complete its A3-style master
  table, order rows by implementation sequence, and keep it current through
  verification and merge. Do not begin implementation until the work document
  identifies the problem, current implementation, desired solution, task
  details, branch, and status. This rule applies to every agent and every
  repository task; a more specific `AGENTS.md` may add requirements but may not
  waive the work definition.
- Move or copy a work document to `docs/development/work/<work-name>.md` only
  when it needs to be shared with contributors, reviewed in a pull request,
  retained as a durable execution/audit record, or explicitly requested by the
  user. Otherwise keep it ignored under root `.work/`. Once a plan is promoted,
  the tracked copy is authoritative and must travel with the implementation.
- Inspect `git status` before editing and preserve unrelated user changes.
- Keep behavior changes, file moves, and formatting-only changes separate so
  each can be reviewed and reverted independently.
- Prefer the smallest change that satisfies the request. Do not introduce a
  new dependency, change a public cache/update format, or alter release paths
  without calling out the impact.
- Use project tooling and existing abstractions before adding parallel ones.
- Never commit generated caches, virtual environments, coverage output, build
  artifacts, downloaded maps, or private signing keys.

## Shared run configurations

- Treat every file under `.run/` as shared, cross-machine project
  configuration. Changes must work from an arbitrary checkout location and
  must not contain a contributor's username, home directory, absolute checkout
  path, personal interpreter path, credentials, tokens, or other machine-local
  state.
- Use JetBrains macros such as `$PROJECT_DIR$`, repository-relative arguments,
  module SDK selection, and portable commands instead of hardcoded filesystem
  paths. Keep personal run configurations and environment values in ignored
  IDE state rather than `.run/`.
- When changing `.run/`, parse the edited files as XML and audit the directory
  for absolute paths, user-specific paths, and secrets before handoff.

## Architecture and compatibility

- Follow the boundaries and dependency direction in
  `docs/development/architecture.md`.
- Follow the stable path contracts in `docs/development/repository-layout.md`.
- Do not add a new dependency, change a public cache/update format, alter a
  release path, or move an externally consumed file without documenting the
  compatibility impact and updating validation in the same change.

## Common commands

Set up the development environment:

```bash
./scripts/dev/install.sh
```

Run the complete suite:

```bash
.venv-dev/bin/python -m pytest -p no:cacheprovider -q
```

Run a focused test file while iterating:

```bash
.venv-dev/bin/python -m pytest -p no:cacheprovider -q tests/path/to/test_file.py
```

Check syntax without writing bytecode into the repository:

```bash
PYTHONPYCACHEPREFIX=/tmp/caveviewer-pycache \
  .venv-dev/bin/python -m compileall -q src/caveviewer
```

## Definition of done

- Update the active work document, wherever it is stored, with final task
  status, verification evidence, PR/merge references, and any remaining
  external action.
- Add or update tests for observable behavior and failure cleanup.
- Update comments in the code clearly outlining what new code does
- Run focused tests first and the complete suite before handoff when practical.
- Run `git diff --check` and inspect the final diff for unrelated changes.
- Update user or developer documentation when commands, configuration,
  architecture, screenshots, or release behavior change.
- Report what was verified and any platform-specific validation that remains.
