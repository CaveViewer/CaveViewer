# Source instructions

Applies to: `src/`
Inherits: `/AGENTS.md`
Overrides: none
Validation:

```bash
PYTHONPYCACHEPREFIX=/tmp/caveviewer-pycache \
  .venv/bin/python -m compileall -q src/caveviewer
```

These rules supplement the repository-level instructions for source files.
Package boundaries, data flow, and threading ownership are defined in
`/docs/development/architecture.md`; do not restate those rules here.

## Module naming

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

## Source changes

- Keep source changes aligned with `/docs/development/architecture.md` and
  `/docs/development/repository-layout.md`.
- Use `git mv` for module renames and update imports, tests, documentation,
  and coverage configuration in the same mechanical change.
- Keep policy separate from side effects as described in
  `/docs/development/coding-standards.md`.
- Do not commit generated source artifacts, `__pycache__`, local cache data, or
  build output under `src/`.
