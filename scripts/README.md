# Scripts Overview

This directory contains build, packaging, and release scripts.

## Main Entry Point

Use the dispatcher script:

```bash
release.sh --target=<target> --version=<version> --notes=<notes> --action=<action> [options]
release.sh [--help]
release.sh --target=<target> --help
```

Targets:

- `all`
- `macos`
- `windows`
- `linux-arm64`
- `linux-x86`

Actions:

- `build`: create an intermediate app bundle
- `package`: create a distributable artifact
- `release`: publish/upload artifacts and write update manifests

Examples:

```bash
release.sh --target=linux-arm64 --version=1.2.45 --notes "Release 1.2.45" --action=build
release.sh --target=linux-arm64 --version=1.2.45 --notes "Release 1.2.45" --action=package
release.sh --target=linux-arm64 --version=1.2.45 --notes "Release 1.2.45" --action=release
release.sh --target=linux-arm64 --version=1.2.45 --notes "Alpha." --action=release --pre-release
release.sh --target=linux-arm64,linux-x86 --version=1.2.45 --notes "Release 1.2.45" --action=package
release.sh --target=all,linux-arm64 --version=1.2.45 --notes "Release 1.2.45" --action=release
```

## All Target

`--target` accepts a single target or a comma-separated list. If `all` appears
anywhere in the list, it takes precedence and all platforms are selected.

Option:

- `--rebuild`
- `--pre-release`

Examples:

```bash
release.sh --target=linux-arm64,linux-x86 --version=1.2.45 --notes "Release 1.2.45" --action=build
release.sh --target=linux-x86 --version=1.2.45 --notes "Release 1.2.45" --action=package
release.sh --target=macos,linux-arm64 --version=1.2.45 --notes "Release 1.2.45" --action=release
release.sh --target=all,linux-arm64 --version=1.2.45 --notes "Release 1.2.45" --action=package
```

## Directory Layout

- `scripts/common`: shared helpers
- `scripts/macos`: macOS build/package/publish scripts
- `scripts/linux/common`: shared Linux build/package/publish internals
- `scripts/linux/arm64`: Linux ARM64 entry points
- `scripts/linux/x86_64`: Linux x86_64 entry points
- `scripts/windows`: Windows build/package/publish scripts
- `scripts/dev`: developer bootstrap scripts

## Build Virtual Environments

Packaging scripts use platform-isolated virtual environments by default.

- macOS build: `.venv-macos-build`
  - Override with `CAVEVIEWER_MACOS_BUILD_VENV=/path/to/venv`
- Linux Docker build:
  - Default template: `.venv-linux-build-{arch}`
  - Override with `CAVEVIEWER_LINUX_BUILD_VENV=/path/with-{arch}-token`

Developer setup remains independent:

- `./scripts/dev/install.sh` uses `.venv-dev` for local app development/runtime.
- Override with `CAVEVIEWER_DEV_VENV=/path/to/venv`.

For Linux-specific packaging details, see `scripts/linux/README.md`.
