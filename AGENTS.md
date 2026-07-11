# CaveViewer repository instructions

These instructions apply to the whole repository. A more specific `AGENTS.md`
inside a subdirectory supplements these rules for that area.

## Sources of truth

- `docs/development/architecture.md` defines component boundaries and
  dependency direction.
- `docs/development/repository-layout.md` defines the current package layout and
  stable external paths. Structural migrations must be explicit, mechanical
  changes.
- `docs/development/coding-standards.md` and
  `docs/development/testing.md` define implementation and verification rules.
- `README-developer.md` remains the detailed setup, packaging, and release
  reference until that material is deliberately split into focused documents.

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

## Architecture constraints

- `caveviewer.core` contains parsing, cache, streaming, and other non-UI
  policies. It must not import `caveviewer.gui`.
- `caveviewer.gui` owns Tk and OpenGL presentation and may depend on core. OpenGL and Tk
  operations stay on their owning main thread; background workers may prepare
  CPU data only.
- Platform-specific behavior belongs behind `caveviewer.gui.platform` adapters rather
  than scattered `sys.platform` branches.
- Failed imports must not publish partial caches. Preserve staging-directory
  cleanup and atomic publication semantics.
- Runtime chunk streaming and longitudinal cross-section generation are
  independent pipelines. Changes to shared cache data must test both.
- Files in `updates/` and the location of `docs/index.html` are externally
  consumed paths. Leave them stable unless the migration explicitly updates
  their consumers.

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
- Run focused tests first and the complete suite before handoff when practical.
- Run `git diff --check` and inspect the final diff for unrelated changes.
- Update user or developer documentation when commands, configuration,
  architecture, screenshots, or release behavior change.
- Report what was verified and any platform-specific validation that remains.
