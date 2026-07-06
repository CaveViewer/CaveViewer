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
- Targets default to all: `macos`, `linux-arm64`, `linux-x86_64`, and `windows`.
- On macOS host, Linux targets build via Docker.
- On Linux host, Linux targets build natively when possible or via Docker when needed.
- macOS targets are skipped with a message on non-macOS hosts.

Options:
- --version=X.Y.Z (required; accepts optional leading v; sets APP_VERSION before packaging)
- --targets=macos,linux-arm64,linux-x86_64,windows (default: all; `linux` and `all` are accepted groups)
- --linux-build=auto|native|docker (default: auto)
- --rebuild
- --publish (publish artifacts after build via platform publish scripts)
- --release-notes="text" (used when --publish is set; default: "Release X.Y.Z")
- --help

When `--publish` is set, existing versioned artifacts are reused when available.
Use `--rebuild` to force fresh rebuilds before publishing.

Examples:

```bash
./scripts/release.sh all-package --version=1.2.45
./scripts/release.sh all-package --version=1.2.45 --targets=linux
./scripts/release.sh all-package --version=1.2.45 --targets=linux-x86_64 --linux-build=docker
./scripts/release.sh all-package --rebuild
./scripts/release.sh all-package --version=1.2.45 --targets=macos,linux-arm64
./scripts/release.sh all-package --version=1.2.45 --publish --release-notes="Bug fixes and stability improvements"
```

## Existing Dispatcher Targets

- macos-package
- macos-publish
- macos-dist-layout
- windows-package
- windows-publish
- linux-package
- linux-arm64-publish
- linux-x86_64-publish

Example:

```bash
./scripts/release.sh macos-package
```

## Directory Layout

- scripts/common: Shared helpers (version parsing, artifact metadata, GitHub helper functions)
- scripts/macos: macOS build/package/publish scripts
- scripts/linux/common: Shared Linux build/package helpers
- scripts/linux/arm64: Linux ARM64 platform entrypoints
- scripts/linux/x86_64: Linux x86_64 platform entrypoints
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
