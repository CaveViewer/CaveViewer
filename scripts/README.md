# Scripts Overview

This directory contains build, packaging, and release scripts.

## Main Entry Point

Use the dispatcher script:

```bash
./scripts/release.sh <target> [args...]
```

## Unified Packaging (All Platforms)

Target:

```bash
./scripts/release.sh all-package --version=X.Y.Z [options]
```

This runs a host-aware packaging flow using existing platform scripts.

Default behavior:
- Linux architecture defaults to both.
- On macOS host:
  - Build macOS package
  - Build Linux packages via Docker
  - Build Windows package
- On Linux host:
  - Build Linux package (native or Docker, depending on arch request and Docker availability)
  - Build Windows package
  - Skip macOS package with a message

Options:
- --version=X.Y.Z (required; accepts optional leading v; sets APP_VERSION before packaging)
- --linux-arch=arm64|amd64|both (default: both)
- --rebuild
- --publish (publish artifacts after build via platform publish scripts)
- --release-notes="text" (used when --publish is set; default: "Release X.Y.Z")
- --skip=macos,linux,windows
- --help

When `--publish` is set, existing versioned artifacts are reused when available.
Use `--rebuild` to force fresh rebuilds before publishing.

Examples:

```bash
./scripts/release.sh all-package --version=1.2.45
./scripts/release.sh all-package --linux-arch=both
./scripts/release.sh all-package --rebuild
./scripts/release.sh all-package --skip=windows
./scripts/release.sh all-package --version=1.2.45 --publish --release-notes="Bug fixes and stability improvements"
```

## Existing Dispatcher Targets

- macos-package
- macos-publish
- macos-dist-layout
- windows-package
- windows-publish
- linux-package
- linux-publish

Example:

```bash
./scripts/release.sh macos-package
```

## Directory Layout

- scripts/common: Shared helpers (version parsing, artifact metadata, GitHub helper functions)
- scripts/macos: macOS build/package/publish scripts
- scripts/linux: Linux build/package/publish scripts (including Docker cross-build)
- scripts/windows: Windows package/publish scripts
- scripts/dev: Developer bootstrap scripts

## Build Virtual Environments

Packaging scripts now use platform-isolated virtual environments by default to
avoid cross-platform overwrites.

- macOS build: `.venv-macos-build`
  - Override with `CAVEVIEWER_MACOS_BUILD_VENV=/path/to/venv`
- Linux native build (auto-detected arch):
  - arm64: `.venv-linux-build-arm64`
  - amd64: `.venv-linux-build-amd64`
  - Override with `CAVEVIEWER_LINUX_BUILD_VENV=/path/to/venv`
- Linux Docker multi-arch build:
  - Default template: `.venv-linux-build-{arch}`
  - Override with `CAVEVIEWER_LINUX_BUILD_VENV=/path/with-{arch}-token`

Developer setup remains independent:
- `./scripts/dev/install.sh` uses `.venv-dev` for local app development/runtime.
- Override with `CAVEVIEWER_DEV_VENV=/path/to/venv`.

For Linux-specific packaging details, see scripts/linux/README.md.
