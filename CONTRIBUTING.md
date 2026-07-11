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

## Release contributions

Read the canonical [release guide](docs/development/releases.md) before changing
release workflows, packaging scripts, update manifests, or version handling.
Release versions must contain only dot-separated decimal integers, such as
`1.0.64`. Do not encode prerelease status in a suffix such as `1.0.64-rc1`;
the update checker treats that form as an unparseable version and will not offer
it as a newer update. Use the workflow's `pre_release` option and the
`prerelease.json` channel instead. GitHub workflow inputs also require the bare
version without a leading `v`.

## Repository layout

The project uses the conventional `src/caveviewer` package layout. Its stable
paths and completed migration sequence are documented in
[repository-layout.md](docs/development/repository-layout.md). Use the current
paths and do not create a second parallel package tree.
