# Linux AppImage Release Builds

Linux release artifacts are built only through Docker. The architecture wrappers
and `release.sh` targets all route into `scripts/linux/build_linux_in_docker.sh`;
the `scripts/linux/common/*` build scripts are internal container entry points
and refuse direct host execution.

## Prerequisites

- Docker installed and running on the release machine
- GitHub CLI authenticated when publishing with `scripts/linux/*/publish.sh`
- Release signing environment configured when publishing signed manifests

Docker provides the Linux build environment, build dependencies, portable Python
tooling, PyInstaller, and AppImage packaging tools. Do not create a host Linux
venv for release builds.

## Build Targets

Build a PyInstaller app bundle for one architecture:

```bash
./scripts/release.sh --target=linux-arm64 --version=1.2.45 --notes "Release 1.2.45" --action=build
./scripts/release.sh --target=linux-x86 --version=1.2.45 --notes "Release 1.2.45" --action=build
```

Package an existing app bundle as an AppImage:

```bash
./scripts/release.sh --target=linux-arm64 --version=1.2.45 --notes "Release 1.2.45" --action=package
./scripts/release.sh --target=linux-x86 --version=1.2.45 --notes "Release 1.2.45" --action=package
```

Build and package selected targets through the orchestrator:

```bash
./scripts/release.sh --target=linux-arm64 --version=1.2.45 --notes "Release 1.2.45" --action=package
./scripts/release.sh --target=linux-x86 --version=1.2.45 --notes "Release 1.2.45" --action=package
./scripts/release.sh --target=linux-arm64,linux-x86 --version=1.2.45 --notes "Release 1.2.45" --action=release
./scripts/release.sh --target=linux-arm64 --version=1.2.45 --notes "Alpha." --action=release --pre-release
```

## Direct Docker Driver

For debugging the Linux release container itself, call the Docker driver:

```bash
./scripts/linux/build_linux_in_docker.sh --arch=arm64 --step=all
./scripts/linux/build_linux_in_docker.sh --arch=x86_64 --step=package
./scripts/linux/build_linux_in_docker.sh --arch=both --step=all --rebuild
```

Supported `--step` values are `build`, `package`, and `all`.

## Publishing

Each Linux architecture publishes through its own wrapper:

```bash
./scripts/linux/arm64/publish.sh 1.2.45 "Release 1.2.45"
./scripts/linux/x86_64/publish.sh 1.2.45 "Release 1.2.45"
```

Those wrappers set the manifest architecture and call the common publisher,
which builds through Docker unless `--skip-build` is supplied.

## Outputs

```text
dist/linux/
├── arm64/
│   ├── app/CaveViewer/
│   └── packages/CaveViewer-1.2.45-aarch64.AppImage
└── x86_64/
    ├── app/CaveViewer/
    └── packages/CaveViewer-1.2.45-x86_64.AppImage
```

Update manifests are architecture-specific:

```text
updates/linux/arm64/stable.json
updates/linux/arm64/stable.json.sig
updates/linux/x86_64/stable.json
updates/linux/x86_64/stable.json.sig
```

## Notes

- `scripts/linux/common/build.sh` and `scripts/linux/common/package.sh` are not
  public entry points.
- `CAVEVIEWER_LINUX_BUILD_VENV` may override the host-side cached build venv
  template used by Docker. The default is `.venv-linux-build-{arch}`.
- `--rebuild` rebuilds the Docker image and clears the matching cached Linux
  build venv.
