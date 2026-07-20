# Source instructions

These rules supplement the repository-level `AGENTS.md` for files under `src/`.
More specific `AGENTS.md` files under source subdirectories add rules for those
areas.

## Quick navigation

- [Architecture boundaries](#architecture-boundaries)
- [Module naming conventions](#module-naming-conventions)
- [Threading and rendering ownership](#threading-and-rendering-ownership)
- [Source changes](#source-changes)

## Architecture boundaries

- `caveviewer.core` contains parsing, cache, streaming, preferences, scheduling,
  and other non-UI policies. It must not import `caveviewer.gui`, Tk, or
  concrete OpenGL APIs.
- `caveviewer.gui` owns Tk and OpenGL presentation and may depend on core.
- Cross-boundary behavior should use callbacks, queues, value objects, or small
  interfaces rather than importing a higher-level package from a lower-level
  package.
- Platform-specific presentation behavior belongs behind
  `caveviewer.gui.platform` adapters rather than scattered `sys.platform`
  branches.

## Module naming conventions

- Use lowercase `snake_case.py` filenames for Python modules.
- Choose module filenames using this priority order:
  - Domain noun, such as `geometry.py`, `camera.py`, or `materials.py`.
  - Component noun, such as `renderer.py`, `loader.py`, or `scheduler.py`.
  - Specific noun phrase, such as `scene_graph.py` or `vertex_buffer.py`.
  - Verb or gerund for a clear workflow, such as `export.py` or
    `validation.py`.
- In a flat package, include enough domain context to avoid ambiguous module
  names, such as `chunk_geometry.py` instead of a generic `geometry.py` when
  multiple domains may eventually have geometry code.
- In a subpackage, avoid repeating the package domain in every filename when
  the package already provides the context, such as `streaming/scheduler.py`
  instead of `streaming/streaming_scheduler.py`.
- Avoid vague catch-all names such as `utils.py`, `helpers.py`, `common.py`,
  or `misc.py`; prefer the smallest clear domain or component name.
- Use `git mv` for module renames and update imports, tests, documentation,
  and coverage configuration in the same mechanical change.

## Threading and rendering ownership

- Tk widget mutations and OpenGL resource creation, upload, modification, and
  deletion must run only on the thread that owns those resources.
- Background workers may do disk I/O, parsing, decompression, image decode, and
  CPU-side preparation, but must not issue arbitrary OpenGL commands.
- Transfer prepared CPU-side data to the render/main thread before GPU upload.
- Do not assume Python's Global Interpreter Lock makes shared application state
  thread-safe.
- Prefer immutable data, message passing, queues, and ownership transfer over
  direct shared-state mutation across threads.

## Source changes

- Keep source changes aligned with the documented package boundaries in
  `docs/development/architecture.md` and
  `docs/development/repository-layout.md`.
- Do not commit generated source artifacts, `__pycache__`, local cache data, or
  build output under `src/`.
- When moving modules, update source imports, tests, documentation, coverage
  configuration, and compatibility shims as part of the same mechanical change.
