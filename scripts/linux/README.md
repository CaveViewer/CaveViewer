# Linux AppImage Release Builds

Linux release artifacts are built only through Docker. Prefer the top-level
`scripts/release.sh` dispatcher for normal release work. The Linux x86_64
release target routes into `scripts/linux/build_linux_in_docker.sh`, which is
the host-side Docker driver.

`scripts/linux/common/build.sh` and `scripts/linux/common/package.sh` are
internal container entry points and refuse direct host execution.
`scripts/linux/common/publish.sh` and `scripts/linux/common/update_manifest.sh`
are shared publisher helpers used by the x86_64 wrapper.

## Prerequisites

- Docker installed and running on the release machine
- GitHub CLI authenticated when publishing with `scripts/linux/x86_64/publish.sh`
- `CAVEVIEWER_RELEASE_SIGNING_PRIVATE_KEY` set when publishing signed manifests

Docker provides the Linux build environment, build dependencies, portable Python
tooling, PyInstaller, and AppImage packaging tools. The Docker driver maintains
a host-side cached build venv under `.venv-linux-build-{arch}` by default; do
not create or activate a separate host Linux venv for release builds.

## Build Target

Build the PyInstaller app bundle:

```bash
./scripts/release.sh --target=linux-x86_64 --version=1.2.45 --notes "Release 1.2.45" --action=build
```

Package an existing app bundle as an AppImage:

```bash
./scripts/release.sh --target=linux-x86_64 --version=1.2.45 --notes "Release 1.2.45" --action=package
```

Build, package, and publish the Linux target:

```bash
./scripts/release.sh --target=linux-x86_64 --version=1.2.45 --notes "Release 1.2.45" --action=release
./scripts/release.sh --target=linux-x86_64 --version=1.2.45 --notes "Alpha." --action=release --pre-release
```

`--action=package` packages an existing Linux app bundle. Run
`--action=build` first if `dist/linux/<arch>/app/CaveViewer` does not exist.
`--action=release` publishes artifacts and writes signed update manifests.
Stable releases write `stable.json`; `--pre-release` marks the GitHub release
as a prerelease and writes `prerelease.json` instead. Both require
`CAVEVIEWER_RELEASE_SIGNING_PRIVATE_KEY`.

## Direct Docker Driver

For debugging the Linux release container itself, call the Docker driver:

```bash
./scripts/linux/build_linux_in_docker.sh --arch=x86_64 --step=package
./scripts/linux/build_linux_in_docker.sh --step=all --rebuild
```

The only supported `--arch` value is `x86_64`.
Supported `--step` values are `build`, `package`, and `all`.

## Publishing

Linux publishes through the x86_64 wrapper:

```bash
./scripts/linux/x86_64/publish.sh --version=1.2.45 --notes "Release 1.2.45"
```

The wrapper sets the manifest architecture and calls the common publisher,
which builds through Docker unless `--use-existing-artifacts` is supplied. Use
`--use-existing-artifacts` only when the matching AppImage already exists under
`dist/linux/<arch>/packages`.

```bash
./scripts/linux/x86_64/publish.sh --version=1.2.45 --notes "Release 1.2.45" --use-existing-artifacts
```

## Outputs

```text
dist/linux/
└── x86_64/
    ├── app/CaveViewer/
    └── packages/CaveViewer-1.2.45-x86_64.AppImage
```

Update manifests are architecture-specific:

```text
updates/linux/x86_64/stable.json
updates/linux/x86_64/stable.json.sig
updates/linux/x86_64/prerelease.json
updates/linux/x86_64/prerelease.json.sig
```

## Notes

- Scripts use named options only; positional arguments are rejected.
- `scripts/linux/common/build.sh` and `scripts/linux/common/package.sh` are not
  public entry points.
- `CAVEVIEWER_LINUX_BUILD_VENV` may override the host-side cached build venv
  template used by Docker. The default is `.venv-linux-build-{arch}`.
- `--rebuild` rebuilds the Docker image and clears the matching cached Linux
  build venv.
- `--pre-release` publishes prerelease assets and advances
  `updates/linux/x86_64/prerelease.json`, leaving `stable.json` untouched.
