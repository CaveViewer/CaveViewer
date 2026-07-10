# Contributing to CaveViewer

Thank you for improving CaveViewer. Changes should remain usable on Windows,
macOS, and Linux and should preserve the ability to work with maps much larger
than system memory.

## Start here

1. Follow the setup instructions in [README-developer.md](README-developer.md).
2. Read the [architecture](docs/development/architecture.md) and
   [repository-layout](docs/development/repository-layout.md) documents before
   moving modules or changing component boundaries.
3. Follow the [coding standards](docs/development/coding-standards.md) and
   [testing guide](docs/development/testing.md).
4. AI coding agents must also follow the nearest `AGENTS.md` file.

## Change workflow

- Start from a clean understanding of `git status`; do not fold unrelated local
  edits into your change.
- Keep behavior changes separate from directory moves and mechanical renames.
- Add a focused regression test for fixes and tests for expected failure
  cleanup.
- Run focused tests while iterating, then run the complete suite:

  ```bash
  .venv-dev/bin/python -m pytest -p no:cacheprovider -q
  ```

- Run `git diff --check` and review the complete diff before submitting.
- Update documentation and screenshots when user-visible behavior changes.

## Repository migration

The project is preparing for a conventional `src/caveviewer` package layout.
The target and migration constraints are documented in
[repository-layout.md](docs/development/repository-layout.md). Until a migration
step is explicitly underway, use the current paths and do not create a second
parallel package tree.
