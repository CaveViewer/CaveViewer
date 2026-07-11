# Scripts Overview

This directory contains build, packaging, and release scripts.

For script CLI conventions and naming rules, see `STANDARDS.md`.
For the canonical release sequence, channel behavior, resume procedure, and
post-release checklist, see `../docs/development/releases.md`.

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
- `linux-x86_64`

Actions:

- `build`: create an intermediate app bundle
- `package`: create a distributable artifact
- `release`: publish/upload artifacts and write update manifests

Release versions must contain only dot-separated decimal integers, such as
`1.0.64`. Do not use suffixes such as `1.0.64-rc1`; the update checker cannot
compare them and will not offer the release as a newer update. Select
`--pre-release` to publish on the prerelease channel instead. The local
dispatcher accepts an optional leading `v`, but GitHub workflow inputs require
the bare numeric version.

Before any action changes the version or invokes a builder, `release.sh` runs
the complete pytest suite with `-p no:cacheprovider -q`. A failing or missing
test environment stops the release. The interpreter is selected in this order:
`CAVEVIEWER_TEST_PYTHON`, `.venv-dev`, `python3`, then `python`.

`--skip-tests` bypasses this local gate. It is intended for orchestrators such
as the GitHub release workflows that require an equivalent test job for the
same application source; normal direct releases should not use it.

Examples:

```bash
release.sh --target=linux-x86_64 --version=1.2.45 --notes "Release 1.2.45" --action=build
release.sh --target=linux-x86_64 --version=1.2.45 --notes "Release 1.2.45" --action=package
release.sh --target=linux-x86_64 --version=1.2.45 --notes "Release 1.2.45" --action=release
release.sh --target=linux-x86_64 --version=1.2.45 --notes "Alpha." --action=release --pre-release
release.sh --target=all --version=1.2.45 --notes "Release 1.2.45" --action=release
```

## Target Selection

`--target` accepts a single target or a comma-separated list. If `all` appears
anywhere in the list, it takes precedence. It selects Windows, Linux x86_64,
and the current process architecture for macOS.
Multi-target package and release orchestration is handled by `release.sh`
directly; platform scripts remain the per-target implementation details.
The macOS architecture is part of the target name, matching the Linux target
pattern. Local macOS builds require a process whose architecture matches the
selected target. The `all` target selects the current process architecture for
macOS.

Options:

- `--rebuild`
- `--skip-tests`: bypass the local gate only when the same application source already passed an external test gate
- `--pre-release`: publish GitHub prerelease assets and update `prerelease.json` instead of `stable.json`

Examples:

```bash
release.sh --target=linux-x86_64 --version=1.2.45 --notes "Release 1.2.45" --action=package
release.sh --target=macos-arm64,linux-x86_64 --version=1.2.45 --notes "Release 1.2.45" --action=release
release.sh --target=macos-arm64 --version=1.2.45 --notes "Release 1.2.45" --action=package
release.sh --target=all --version=1.2.45 --notes "Release 1.2.45" --action=package
```

## GitHub Actions

Release workflows live under `.github/workflows/` for macOS 15, Windows, and
Linux x86_64. Each platform workflow can be dispatched directly or called by
another workflow. A directly dispatched platform workflow runs the shared
essential test suite before invoking `release.sh`. The internal
`skip_essential_tests` reusable-workflow input is not exposed by manual
dispatch and is reserved for a caller that provides the equivalent test gate.

macOS has separate ARM64 and x86_64 workflows, matching the dispatcher target
names. ARM64 runs on `macos-15`; Intel runs on `macos-15-intel`. Artifacts are
named `CaveViewer-<version>-macos-<architecture>.dmg`.

The `All Platform Release` workflow runs the shared essential test suite once,
then starts the Windows, Linux x86_64, macOS ARM64, and macOS x86_64
build/package jobs in parallel. Each job checks out the same source
commit, produces its platform package without publishing, and uploads its
binary plus any package metadata as a workflow artifact. Linux jobs run
`release.sh --action=build` before `--action=package` because the package phase
consumes the intermediate app bundle.

When publishing is enabled, one finalizer waits for every package, downloads
the artifacts, uploads them to one GitHub release, writes and signs all update
manifests, updates the application version, and pushes one commit. The package
jobs are read-only and do not receive the release signing key. Individually
dispatched platform workflows use the same finalizer for their one target.

When dispatching a workflow:

- choose the source branch explicitly;
- enter a bare, dotted-numeric version such as `1.0.64`, without a leading `v`
  or a suffix such as `-rc1`, because workflow artifact paths use the input
  verbatim and the update checker only compares numeric components;
- leave `publish` disabled to build and retain a test artifact only;
- enable both `publish` and `pre_release` to publish a GitHub prerelease and
  update that platform/architecture's `prerelease.json` rather than
  `stable.json`;
- use `All Platform Release` to package all platforms concurrently and publish
  them through one finalizer;
- avoid source pushes to the selected branch while packages are building. The
  finalizer rejects a moved branch rather than publishing metadata for a source
  revision different from the packaged artifacts.

## Directory Layout

- `scripts/common`: shared helpers
- `scripts/common/finalize_release.sh`: internal single-writer CI finalizer
- `scripts/macos`: macOS 15 build/package/publish scripts
- `scripts/linux/common`: shared Linux build/package/publish internals
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
