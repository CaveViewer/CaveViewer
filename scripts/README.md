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
- --skip=macos,linux,windows
- --help

Examples:

```bash
./scripts/release.sh all-package --version=1.2.45
./scripts/release.sh all-package --linux-arch=both
./scripts/release.sh all-package --rebuild
./scripts/release.sh all-package --skip=windows
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

For Linux-specific packaging details, see scripts/linux/README.md.
