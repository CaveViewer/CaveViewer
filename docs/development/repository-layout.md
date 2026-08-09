# Repository layout

## Status

The application uses a standard `src` package layout. Runtime code and bundled
resources live under `src/caveviewer`; the former top-level runtime paths have
been removed rather than kept as compatibility copies.

## Current stable paths

```text
src/caveviewer/app.py                         startup, session loop, UI adapters
src/caveviewer/benchmark.py                   viewer FPS benchmark CLI
src/caveviewer/version.py                     release identity
src/caveviewer/storage_paths.py               XDG/portable application roots
src/caveviewer/benchmarking/                  benchmark scenarios, metrics, comparisons, and routes
src/caveviewer/benchmarking/map_runner.py     local map benchmark orchestration CLI
src/caveviewer/benchmarking/results.py        benchmark scenario/results model and controller
src/caveviewer/benchmarking/routes.py         benchmark-specific route generation
src/caveviewer/core/                          parsing, cache, streaming, and non-UI policy
src/caveviewer/core/capabilities/             immutable cross-layer capability values and hardware snapshots
src/caveviewer/core/capabilities/desktop.py    desktop execution-route values
src/caveviewer/core/capabilities/hardware.py   typed GPU-memory and RAM-availability values
src/caveviewer/core/json_io.py                 bounded JSON loading for core inputs
src/caveviewer/core/map/                      map discovery, cache, and import workflow
src/caveviewer/core/map/cache_paths.py         map-cache location policy
src/caveviewer/core/map/cache_build_lock.py    cooperative per-cache build ownership
src/caveviewer/core/map/cache_identity.py      versioned Guided Dive cache identity
src/caveviewer/core/map/source_model.py        source-format registry, capabilities, and discovery
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
src/caveviewer/core/diagnostics/application.py  process lifecycle and exception diagnostics
src/caveviewer/core/hardware/                 hardware probes and numeric compatibility APIs
src/caveviewer/core/hardware/gpu_memory.py     active-GPU memory capability probes and numeric fallbacks
src/caveviewer/core/hardware/memory_targets.py RAM/GPU utilization target parsing
src/caveviewer/core/hardware/system_memory.py  system-RAM availability probes and total-RAM fallback
src/caveviewer/core/streaming/                 runtime chunk streaming policy
src/caveviewer/core/streaming/world.py         worker lifecycle and render callbacks
src/caveviewer/core/streaming/scheduler.py     backlog, selection, and eviction policy
src/caveviewer/core/streaming/budget.py        typed-memory policy and residency budget calculation
src/caveviewer/core/navigation/                 centerline, curvature, and voxel route policy
src/caveviewer/core/navigation/certificate_build.py explicit cache-bound navigation certificate CLI
src/caveviewer/core/navigation/voxel_cache.py   cache-time voxel graph/index models and summaries
src/caveviewer/core/navigation/voxel_store.py   in-memory and bounded-LRU navigation voxel chunks
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
src/caveviewer/gui/cache_rebuild_controller.py splash-owned forced cache-rebuild lifecycle
src/caveviewer/gui/features/                  pure feature availability policies and gates
src/caveviewer/gui/platform/runtime.py        process-owned platform composition root
src/caveviewer/gui/platform/presentation.py   immutable static GUI presentation profile
src/caveviewer/gui/platform/presentation_actions.py focused native presentation-action facade
src/caveviewer/gui/platform/directory_selection.py shared action-time directory-picker authorization
src/caveviewer/gui/platform/update_package_reveal.py focused verified-package reveal facade
src/caveviewer/gui/platform/update_package_storage.py focused verified-package storage facade
src/caveviewer/gui/platform/saved_recording_reveal.py focused post-save recording reveal facade
src/caveviewer/gui/platform/recording_process.py focused recording-encoder startup facade
src/caveviewer/gui/platform/tls_trust.py       focused native TLS-trust augmentation facade
src/caveviewer/gui/platform/probes/           platform capability probes and configuration
src/caveviewer/gui/platform/probes/desktop.py on-demand directory-selection route probe
src/caveviewer/gui/platform/probes/update_package_reveal.py static verified-package reveal route probe
src/caveviewer/gui/map_library.py             recent-map row display models
src/caveviewer/gui/map_library_sources.py     source-neutral catalog contracts/composition
src/caveviewer/gui/map_library_controller.py  source-qualified library row/transfer state
src/caveviewer/gui/map_library_panel.py       splash Map Library Tk panel
src/caveviewer/gui/map_library_workflow.py    splash Map Library workflow
src/caveviewer/gui/map_cache_rebuild.py        map-local cache-rebuild preflight and target resolution
src/caveviewer/gui/guided_dive_playback.py    map-local Guided Dive preflight and target resolution
src/caveviewer/gui/map_opening.py             shared map-folder chooser and target resolution
src/caveviewer/gui/standard_library_maps.py   GitHub source adapter and managed map storage
src/caveviewer/gui/standard_library_download.py standard-library download workers
src/caveviewer/gui/preferences.py             preference persistence facade
src/caveviewer/gui/preferences_form.py        Tk-free preference form state
src/caveviewer/gui/preferences_dialog.py      Tk preference dialog presentation
src/caveviewer/gui/recording.py               recording encoder process/thread helpers
src/caveviewer/gui/recording_controller.py    recording countdown/status/timing state
src/caveviewer/gui/recording_capture.py       render-thread recording readback resources
src/caveviewer/gui/manual_dive_trace_controller.py manual trace countdown/reveal state
src/caveviewer/gui/benchmark.py               compatibility wrapper for benchmark results
src/caveviewer/gui/benchmark_routes.py        compatibility wrapper for benchmark route generation
src/caveviewer/gui/chunk_upload.py            render-thread chunk upload state and cleanup
src/caveviewer/gui/texture_manager.py         render-thread OpenGL texture lifecycle
src/caveviewer/gui/view_culling.py            resident chunk frustum-culling cache
src/caveviewer/gui/update_manager.py          process-lifetime update state/workers
src/caveviewer/resources/                     shaders, images, and public key
tests/                                        unit and integration tests
benchmarks/                                   versioned benchmark scenario/threshold files
docs/                                         site and development documentation
packaging/pyinstaller/CaveViewer.spec         PyInstaller configuration
packaging/linux/                              desktop and AppStream metadata
scripts/                                      development and release automation
scripts/benchmark/                            benchmark comparison helpers and compatibility wrappers
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
│       ├── benchmark.py
│       ├── storage_paths.py
│       ├── benchmarking/
│       │   ├── map_runner.py
│       │   ├── results.py
│       │   └── routes.py
│       ├── core/
│       │   ├── capabilities/
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
│       │   ├── features/
│       │   └── platform/
│       │       └── probes/
│       └── resources/
│           ├── shaders/
│           ├── images/
│           └── release_signing_public_key.pem
├── tests/
│   ├── unit/
│   └── integration/
├── benchmarks/
├── docs/
│   ├── index.html
│   ├── images/
│   └── development/
├── packaging/
│   ├── linux/
│   └── pyinstaller/
├── scripts/
│   └── benchmark/
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
- Keep `benchmarks/` limited to versioned scenario/configuration files.
  Precompiled benchmark maps and generated benchmark results are external
  artifacts and must stay untracked.
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
