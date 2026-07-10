# Repository layout

## Status

The repository currently runs directly from top-level `caveviewer.py`, `core/`,
and `gui/` paths. The target below is approved direction, not the current
filesystem. Migrate in explicit, behavior-preserving stages; do not maintain
both layouts in parallel.

## Current stable paths

```text
caveviewer.py              application entry point and startup workflow
caveviewer_version.py      release identity
core/                      parsing, cache, streaming, texture policy
gui/                       Tk/OpenGL UI and platform adapters
shaders/                   runtime shader resources
security/                  bundled public verification key
tests/                     unit and integration tests
docs/                      GitHub Pages site and development documentation
scripts/                   development, build, package, and release automation
updates/                   published update manifests and signatures
CaveViewer.spec            PyInstaller configuration
```

Ignored local directories such as `.venv*`, `.cache`, `__pycache__`, `.idea`,
`.run`, build output, and imported map caches are not repository architecture.
They must stay untracked.

## Approved target

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
│       │   ├── importers/
│       │   ├── cache/
│       │   └── streaming/
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

The first package move should create `src/caveviewer/{core,gui,resources}` while
preserving the existing internal module grouping. Splitting `core` into
`importers`, `cache`, and `streaming` is a later architectural change, not part
of the mechanical move.

## Paths that remain stable

- Keep `docs/index.html` and `docs/images/` at the documentation root so the
  existing GitHub Pages publishing layout remains valid.
- Keep `updates/<platform>/...` paths stable because installed clients resolve
  those public URLs.
- Keep `scripts/` organized by `dev`, `common`, and platform; it already has
  documented standards.
- Keep root `README.md`, `CHANGELOG.md`, `LICENSE`, and third-party notices.

## Migration sequence

1. **Governance:** add the repository map, architecture/testing standards, and
   AI-assistant entry points. No runtime paths change.
2. **Package shell:** add project metadata and move the application, `core`, and
   `gui` into `src/caveviewer` with `git mv`. Update imports mechanically; do
   not refactor behavior in this step.
3. **Resources:** move shaders, GUI images, and the public signing key into
   package resources. Centralize lookup with `importlib.resources` or one
   resource service that also supports PyInstaller.
4. **Consumers:** update `CaveViewer.spec`, development launchers, packaging and
   release scripts, CI coverage targets, documentation commands, and tests.
5. **Boundary refactors:** after the moved application is green, split large
   modules such as `chunker.py` and isolate texture decoding from GPU ownership.

Each stage should be independently reviewable and should leave the complete
test suite passing.

## Migration acceptance criteria

- `python -m caveviewer` is the canonical development entry point.
- Development setup installs the package in editable mode rather than editing
  `sys.path` at runtime.
- Source and bundled builds resolve the same package resources through one
  tested mechanism.
- PyInstaller analysis succeeds for every supported platform configuration.
- Update manifest URLs and signature verification remain compatible.
- The complete test suite and configured coverage thresholds pass.
- No generated, IDE-specific, cache, or private-key files become tracked.
