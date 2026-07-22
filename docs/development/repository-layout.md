# Repository layout

## Status

The application uses a standard `src` package layout. Runtime code and bundled
resources live under `src/caveviewer`; the former top-level runtime paths have
been removed rather than kept as compatibility copies.

## Current stable paths

```text
src/caveviewer/app.py                         startup, session loop, UI adapters
src/caveviewer/version.py                     release identity
src/caveviewer/storage_paths.py               XDG/portable application roots
src/caveviewer/core/                          parsing, cache, streaming, and non-UI policy
src/caveviewer/core/json_io.py                 bounded JSON loading for core inputs
src/caveviewer/core/map/                      map discovery, cache, and import workflow
src/caveviewer/core/map/cache_paths.py         map-cache location policy
src/caveviewer/core/map/source_model.py        supported source-model discovery
src/caveviewer/core/map/importer.py            model import and cache-build workflow
src/caveviewer/core/map/compiler.py            CLI map compilation orchestration
src/caveviewer/core/map/chunk_size_advisor.py  chunk-size analysis and recommendations
src/caveviewer/core/chunking/                 chunk cache construction and I/O
src/caveviewer/core/chunking/buckets.py        incremental bucket files and finalization
src/caveviewer/core/chunking/builder.py        chunk cache construction orchestration
src/caveviewer/core/chunking/capacity.py       import capacity and preflight policy
src/caveviewer/core/chunking/io.py             chunk binary file format read/write helpers
src/caveviewer/core/chunking/metadata.py       chunk manifest metadata helpers
src/caveviewer/core/chunking/staging.py        cache staging and import resume checkpoints
src/caveviewer/core/chunking/upload.py         CPU upload preparation and vertex packing
src/caveviewer/core/diagnostics/              diagnostics and logging policy
src/caveviewer/core/diagnostics/logging.py     runtime logging and console progress
src/caveviewer/core/hardware/                 hardware capability and memory policy
src/caveviewer/core/hardware/gpu_memory.py     active-GPU memory detection and fallbacks
src/caveviewer/core/hardware/memory_targets.py RAM/GPU utilization target parsing
src/caveviewer/core/hardware/system_memory.py  system RAM detection
src/caveviewer/core/streaming/                 runtime chunk streaming policy
src/caveviewer/core/streaming/world.py         worker lifecycle and render callbacks
src/caveviewer/core/streaming/scheduler.py     backlog, selection, and eviction policy
src/caveviewer/core/streaming/budget.py        residency budget calculation
src/caveviewer/core/workers/                  worker allocation policy
src/caveviewer/core/workers/allocation.py      CPU caps and RAM admission policy
src/caveviewer/core/preferences/               preference schema and validation policy
src/caveviewer/core/preferences/schema.py       preference field schema and env mapping
src/caveviewer/core/mesh/                      mesh format parsing
src/caveviewer/core/mesh/obj.py                Wavefront OBJ and MTL parsing
src/caveviewer/core/mesh/glb.py                GLB/glTF parsing
src/caveviewer/core/textures/                  worker-safe texture CPU policy
src/caveviewer/core/textures/decoding.py       texture decode, inspection, and budgets
src/caveviewer/gui/                           Tk/OpenGL UI and platform adapters
src/caveviewer/gui/map_library.py             recent-map row display models
src/caveviewer/gui/map_library_controller.py  standard-library row/download state
src/caveviewer/gui/map_library_panel.py       splash Map Library Tk panel
src/caveviewer/gui/map_library_workflow.py    splash Map Library workflow
src/caveviewer/gui/preferences.py             preference persistence facade
src/caveviewer/gui/preferences_form.py        Tk-free preference form state
src/caveviewer/gui/preferences_dialog.py      Tk preference dialog presentation
src/caveviewer/gui/texture_manager.py         render-thread OpenGL texture lifecycle
src/caveviewer/gui/update_manager.py          process-lifetime update state/workers
src/caveviewer/resources/                     shaders, images, and public key
tests/                                        unit and integration tests
docs/                                         site and development documentation
packaging/pyinstaller/CaveViewer.spec         PyInstaller configuration
packaging/linux/                              desktop and AppStream metadata
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
│       ├── storage_paths.py
│       ├── core/
│       │   ├── chunking/
│       │   ├── diagnostics/
│       │   ├── hardware/
│       │   ├── map/
│       │   ├── mesh/
│       │   ├── preferences/
│       │   ├── streaming/
│       │   ├── textures/
│       │   └── workers/
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
│   ├── linux/
│   └── pyinstaller/
├── scripts/
├── updates/
└── .github/
    └── workflows/
```

The current `core` package is intentionally split by domain and component.
Further structural changes remain architectural changes and require their own
tests and review.

## Paths that remain stable

- Keep `docs/index.html` and `docs/images/` at the documentation root so the
  dedicated GitHub Pages workflow can publish `docs/` without transforming or
  copying the site.
- Keep `updates/<platform>/...` paths stable because installed clients resolve
  those public URLs. Windows uses `updates/windows/`; Linux uses
  `updates/linux/x86_64/`; macOS uses `updates/macos/{arm64,x86_64}/`. Retain
  the top-level macOS manifests and signatures as legacy ARM64 compatibility
  aliases.
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
