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

## Working agreement

- Inspect `git status` before editing and preserve unrelated user changes.
- Keep behavior changes, file moves, and formatting-only changes separate so
  each can be reviewed and reverted independently.
- Prefer the smallest change that satisfies the request. Do not introduce a
  new dependency, change a public cache/update format, or alter release paths
  without calling out the impact.
- Use project tooling and existing abstractions before adding parallel ones.
- Never commit generated caches, virtual environments, coverage output, build
  artifacts, downloaded maps, or private signing keys.

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

- Add or update tests for observable behavior and failure cleanup.
- Update comments in the code clearly outlining what new code does
- Run focused tests first and the complete suite before handoff when practical.
- Run `git diff --check` and inspect the final diff for unrelated changes.
- Update user or developer documentation when commands, configuration,
  architecture, screenshots, or release behavior change.
- Report what was verified and any platform-specific validation that remains.
