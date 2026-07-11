# Scripts Overview

This directory contains build, packaging, and release scripts.

For script CLI conventions and naming rules, see `STANDARDS.md`.

## Main Entry Point

Use the dispatcher script:

```bash
release.sh --target=<target> --version=<version> --notes=<notes> --action=<action> [options]
release.sh [--help]
release.sh --target=<target> --help
```

Targets:

- `all`
- `macos-arm64`
- `macos-x86_64`
- `windows`
- `linux-arm64`
- `linux-x86_64`

Actions:

- `build`: create an intermediate app bundle
- `package`: create a distributable artifact
- `release`: publish/upload artifacts and write update manifests

Before any action changes the version or invokes a builder, `release.sh` runs
the complete pytest suite with `-p no:cacheprovider -q`. A failing or missing
test environment stops the release. The interpreter is selected in this order:
`CAVEVIEWER_TEST_PYTHON`, `.venv-dev`, `python3`, then `python`.

`--skip-tests` bypasses this local gate. It is intended for orchestrators such
as the GitHub release workflows that require an equivalent test job for the
same commit; normal direct releases should not use it.

Examples:

```bash
release.sh --target=linux-arm64 --version=1.2.45 --notes "Release 1.2.45" --action=build
release.sh --target=linux-arm64 --version=1.2.45 --notes "Release 1.2.45" --action=package
release.sh --target=linux-arm64 --version=1.2.45 --notes "Release 1.2.45" --action=release
release.sh --target=linux-arm64 --version=1.2.45 --notes "Alpha." --action=release --pre-release
release.sh --target=linux-arm64,linux-x86_64 --version=1.2.45 --notes "Release 1.2.45" --action=package
release.sh --target=all --version=1.2.45 --notes "Release 1.2.45" --action=release
```

## Target Selection

`--target` accepts a single target or a comma-separated list. If `all` appears
anywhere in the list, it takes precedence. It selects Windows, both Linux
architectures, and the current process architecture for macOS.
Multi-target package and release orchestration is handled by `release.sh`
directly; platform scripts remain the per-target implementation details.
The macOS architecture is part of the target name, matching the Linux target
pattern. Local macOS builds require a process whose architecture matches the
selected target. The `all` target selects the current process architecture for
macOS.

Options:

- `--rebuild`
- `--skip-tests`: bypass the local gate only when the same commit already passed an external test gate
- `--pre-release`: publish GitHub prerelease assets and update `prerelease.json` instead of `stable.json`

Examples:

```bash
release.sh --target=linux-arm64,linux-x86_64 --version=1.2.45 --notes "Release 1.2.45" --action=build
release.sh --target=linux-x86_64 --version=1.2.45 --notes "Release 1.2.45" --action=package
release.sh --target=macos-arm64,linux-arm64 --version=1.2.45 --notes "Release 1.2.45" --action=release
release.sh --target=macos-arm64 --version=1.2.45 --notes "Release 1.2.45" --action=package
release.sh --target=all --version=1.2.45 --notes "Release 1.2.45" --action=package
```

## GitHub Actions

Release workflows live under `.github/workflows/` for macOS 15, Windows, Linux
ARM64, and Linux x86_64. Each platform workflow can be dispatched directly or
called by another workflow. A directly dispatched platform workflow runs the
shared essential test suite before invoking `release.sh`. The internal
`skip_essential_tests` reusable-workflow input is not exposed by manual
dispatch and is reserved for a caller that provides the equivalent test gate.

macOS has separate ARM64 and x86_64 workflows, matching the dispatcher target
names. ARM64 runs on `macos-15`; Intel runs on `macos-15-intel`. Artifacts are
named `CaveViewer-<version>-macos-<architecture>.dmg`.

The `All Platform Release` workflow runs every platform workflow in this order:

1. Windows
2. Linux ARM64
3. Linux x86_64
4. macOS ARM64
5. macOS x86_64

The all-platform workflow runs the shared essential test suite once, then calls
each platform workflow with its duplicate test gate disabled. The jobs are
connected with `needs`, so Windows starts only after the shared test gate
succeeds and every later platform starts only after its predecessor succeeds.
Every platform build checks out the latest head of the selected branch. This
preserves the version and manifest commit pushed by each published platform for
the next platform in the chain.

When dispatching a workflow:

- choose the source branch explicitly;
- leave `publish` disabled to build and retain a test artifact only;
- enable both `publish` and `pre_release` to publish a GitHub prerelease and
  update that platform/architecture's `prerelease.json` rather than
  `stable.json`;
- use `All Platform Release` to publish all platforms sequentially;
- do not dispatch a separate platform publish workflow against the same branch
  while the all-platform workflow is running, because every successful publish
  commits its version and manifest update back to that branch.

## Directory Layout

- `scripts/common`: shared helpers
- `scripts/macos`: macOS 15 build/package/publish scripts
- `scripts/linux/common`: shared Linux build/package/publish internals
- `scripts/linux/arm64`: Linux ARM64 entry points
- `scripts/linux/x86_64`: Linux x86_64 entry points
- `scripts/windows`: Windows build/package/publish scripts
- `scripts/dev`: developer bootstrap scripts

## Build Virtual Environments

Packaging scripts use platform-isolated virtual environments by default.

- macOS 15 build: `.venv-macos-build`
  - Override with `CAVEVIEWER_MACOS_BUILD_VENV=/path/to/venv`
- Linux Docker build:
  - Default template: `.venv-linux-build-{arch}`
  - Override with `CAVEVIEWER_LINUX_BUILD_VENV=/path/with-{arch}-token`

Developer setup remains independent:

- `./scripts/dev/install.sh` uses `.venv-dev` for local app development/runtime.
- Override with `CAVEVIEWER_DEV_VENV=/path/to/venv`.

For Linux-specific packaging details, see `scripts/linux/README.md`.
