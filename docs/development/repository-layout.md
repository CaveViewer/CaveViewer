# Repository layout

## Status

The application uses a standard `src` package layout. Runtime code and bundled
resources live under `src/caveviewer`; the former top-level runtime paths have
been removed rather than kept as compatibility copies.

## Current stable paths

```text
src/caveviewer/app.py                         startup and import workflow
src/caveviewer/version.py                     release identity
src/caveviewer/core/                          parsing, cache, and streaming policy
src/caveviewer/gui/                           Tk/OpenGL UI and platform adapters
src/caveviewer/resources/                     shaders, images, and public key
tests/                                        unit and integration tests
docs/                                         site and development documentation
packaging/pyinstaller/CaveViewer.spec         PyInstaller configuration
scripts/                                      development and release automation
updates/                                      published update manifests/signatures
```

Ignored local directories such as `.venv*`, `.cache`, `__pycache__`, `.idea`,
`.run`, build output, and imported map caches are not repository architecture.
They must stay untracked.

## Package layout

```text
CaveViewer/
├── AGENTS.md
├── CONTRIBUTING.md
├── pyproject.toml
├── src/
│   └── caveviewer/
│       ├── __init__.py
│       ├── __main__.py
│       ├── app.py
│       ├── core/
│       ├── gui/
│       │   └── platform/
│       └── resources/
│           ├── shaders/
│           ├── images/
│           └── release_signing_public_key.pem
├── tests/
│   ├── unit/
│   └── integration/
├── docs/
│   ├── index.html
│   ├── images/
│   └── development/
├── packaging/
│   └── pyinstaller/
├── scripts/
├── updates/
└── .github/
    └── workflows/
```

The package move intentionally preserved the existing module grouping. A future
split of `core` into narrower importer, cache, and streaming packages is a
separate architectural change and requires its own tests and review.

## Paths that remain stable

- Keep `docs/index.html` and `docs/images/` at the documentation root so the
  existing GitHub Pages publishing layout remains valid.
- Keep `updates/<platform>/...` paths stable because installed clients resolve
  those public URLs. macOS uses `updates/macos/{arm64,x86_64}/`; retain the
  top-level macOS manifests as legacy ARM64 compatibility aliases.
- Keep `scripts/` organized by `dev`, `common`, and platform; it already has
  documented standards.
- Keep root `README.md`, `CHANGELOG.md`, `LICENSE`, and third-party notices.

## Completed migration sequence

1. **Governance:** add the repository map, architecture/testing standards, and
   AI-assistant entry points. No runtime paths change.
2. **Package shell:** add project metadata and move the application, `core`, and
   `gui` into `src/caveviewer` with `git mv`. Update imports mechanically.
3. **Resources:** move shaders, GUI images, and the public signing key into
   package resources. Centralize lookup with `importlib.resources`.
4. **Consumers:** move the PyInstaller spec under `packaging/pyinstaller` and
   update launchers, release scripts, CI coverage paths, documentation, and
   tests.

Boundary refactors, such as splitting large modules, are deliberately outside
this mechanical migration.

Each stage should be independently reviewable and should leave the complete
test suite passing.

## Layout contract

- `python -m caveviewer` is the canonical development entry point.
- Development setup installs the package in editable mode rather than editing
  `sys.path` at runtime.
- Source and bundled builds resolve the same package resources through one
  tested mechanism.
- PyInstaller inputs resolve the package and resources for every supported
  platform configuration.
- Update manifest URLs and signature verification remain compatible.
- Path-contract tests, the complete suite, and configured coverage thresholds
  pass.
- No generated, IDE-specific, cache, or private-key files become tracked.
