# Releases

This document is the canonical release checklist for CaveViewer. Script CLI
details remain in [`scripts/README.md`](../../scripts/README.md), while update
configuration and local packaging variables remain in
[`README-developer.md`](../../README-developer.md).

## Release matrix

CaveViewer publishes five user-installable artifacts from five architecture-
specific GitHub workflows:

| Target | Workflow | Runner | User artifact |
|---|---|---|---|
| `windows` | `Windows Release` | `windows-latest` | `CaveViewer-<version>-windows.zip` |
| `linux-arm64` | `Linux ARM64 Release` | `ubuntu-24.04-arm` | `CaveViewer-<version>-aarch64.AppImage` |
| `linux-x86_64` | `Linux x86_64 Release` | `ubuntu-latest` | `CaveViewer-<version>-x86_64.AppImage` |
| `macos-arm64` | `macOS ARM64 Release` | `macos-15` | `CaveViewer-<version>-macos-arm64.dmg` |
| `macos-x86_64` | `macOS x86_64 Release` | `macos-15-intel` | `CaveViewer-<version>-macos-x86_64.dmg` |

The Windows ZIP is a guided source/setup bundle. Its root `launch.bat` starts
`setup.ps1`, which installs Python when needed, installs the required Python
packages, prepares the Visual C++ runtime, configures outbound firewall access
when permitted, and creates a desktop shortcut. The DMGs and AppImages are
bundled applications.

GitHub automatically provides its own source-code ZIP and tarball for each tag.
`scripts/common/package_source.sh` can create
`CaveViewer-<version>-source.tar.gz` locally, but the current release workflows
do not upload that tarball as a release asset.

## Channels and update paths

GitHub workflow inputs must use a bare version such as `1.0.64`, not
`v1.0.64`. Artifact upload paths use that input verbatim. Local release scripts
normalize an optional leading `v`, and GitHub uses the tag `v<version>`.

- A stable publish updates `stable.json`. A newly created GitHub release is a
  normal release.
- A prerelease publish updates `prerelease.json`. A newly created GitHub
  release is marked as a prerelease.
- Uploading to an existing tag does not change its release notes or
  prerelease/latest status.
- Stable and prerelease manifests are independent. Publishing one channel must
  not overwrite the other channel.

Published manifest paths are:

```text
updates/windows/<stable|prerelease>.json
updates/linux/arm64/<stable|prerelease>.json
updates/linux/x86_64/<stable|prerelease>.json
updates/macos/arm64/<stable|prerelease>.json
updates/macos/x86_64/<stable|prerelease>.json
```

Every platform publishes a matching `.json.sig` beside each manifest. macOS
ARM64 publishing also copies the signed files to the legacy aliases at
`updates/macos/<stable|prerelease>.json[.sig]`. Those aliases must remain
byte-for-byte identical to the ARM64 files. `.gitattributes` forces every
update JSON file to use LF line endings so Git cannot rewrite signed manifest
bytes during a Windows checkout or commit.

The application checks architecture-specific manifests from the selected
branch and channel; it does not derive updates from GitHub's “latest release”
metadata. It verifies a newer manifest's Ed25519 signature before offering its
artifact, then verifies the artifact size and SHA-256 while downloading.

## Recommended GitHub release

Use [`.github/workflows/all-platform-release.yml`](../../.github/workflows/all-platform-release.yml)
for a complete release.

1. Open **Actions → All Platform Release → Run workflow**.
2. Select the source branch explicitly. For a production release this is
   normally `main`.
3. Enter the bare version (for example, `1.0.64`, not `v1.0.64`) and release
   notes that apply to every platform.
4. Leave `publish` off for package-only validation. Actions retains each build
   as a workflow artifact, but no GitHub release or update manifest is changed.
5. Turn `publish` on to upload assets and commit update metadata.
6. Turn `pre_release` on only when the tag must be a GitHub prerelease and the
   `prerelease.json` channel must be updated.

The workflow runs the shared Essential Tests gate once, then fans out five
package jobs from the same immutable source revision:

```text
                    ┌─ Windows ────────┐
                    ├─ Linux ARM64 ────┤
Essential Tests ────├─ Linux x86_64 ───┼─ Finalize Release
                    ├─ macOS ARM64 ────┤
                    └─ macOS x86_64 ───┘
```

Each called platform workflow skips its duplicate internal gate. A platform
workflow dispatched on its own still runs Essential Tests before packaging.
Normal pushes to `main` or `release/**` also trigger `.github/workflows/tests.yml`;
those branch-CI runs are separate from the single gate inside All Platform
Release.

GitHub requires write permission to be preserved across each reusable-workflow
call boundary because a nested workflow cannot elevate permissions later. The
test and build/package jobs explicitly downgrade their own tokens to read-only
and never publish directly. Linux jobs run the separate build phase before
packaging because their package phase consumes an existing app bundle. The jobs
upload their binaries and package metadata as workflow artifacts; they do not
create GitHub releases, receive the signing key, write manifests, or push
commits. If every requested package succeeds and `publish` is enabled, the
finalizer uses the preserved write permission to download all artifacts, create
or update the GitHub release once, write and sign every requested manifest,
update `src/caveviewer/version.py`, and push one metadata commit.

Every package checks out the workflow's starting commit rather than a moving
branch head. Before publishing, the finalizer verifies that the selected branch
still points to that commit. A concurrent source push therefore fails the
release safely instead of mixing artifacts and metadata from different source
revisions. Finalizers from complete and individual platform workflows share a
branch-level concurrency lock.

The repository secret `CAVEVIEWER_RELEASE_SIGNING_PRIVATE_KEY` must contain the
Ed25519 private key used for update manifests. Only the finalizer receives this
secret. Package-only runs do not require it.

## Existing tags and prerelease promotion

The release finalizer creates the tag/release only when it does not already
exist. When it does exist, it uploads with `--clobber`; it does not change the
release notes or prerelease/latest status.

This matters when the same version is first published as a prerelease and later
promoted to stable. Uploading stable assets and manifests is not enough. After
all stable platform jobs succeed, edit that tag on GitHub, clear the prerelease
flag, publish it as the normal release, and verify that
[`releases/latest`](https://github.com/KernalPanic/CaveViewer/releases/latest)
resolves to it.

## Individual and resumed releases

The five platform workflows remain manually dispatchable. A direct workflow
runs its package job and, when `publish` is enabled, calls the same single-writer
finalizer for that platform. Use the same source branch, version, release notes,
`publish`, and `pre_release` values when resuming an intentionally partial
release.

An all-platform build failure does not publish any platform or manifest because
the finalizer requires all five package jobs to succeed. Inspect the retained
workflow artifacts, correct the failure, and rerun All Platform Release. A
failed finalizer can be rerun safely while the selected branch still points to
the original source commit: existing release assets are replaced by name and
the metadata commit is attempted again.

## Local dispatcher

Use [`scripts/release.sh`](../../scripts/release.sh) for local build, package,
and publish operations:

```bash
./scripts/release.sh \
  --target=linux-x86_64 \
  --version=1.0.64 \
  --notes="Release notes" \
  --action=package
```

Targets are `windows`, `linux-arm64`, `linux-x86_64`, `macos-arm64`,
`macos-x86_64`, and `all`. A comma-separated target list is also accepted.
Unlike the GitHub workflows, the local dispatcher accepts an optional leading
`v` in the version.

Both macOS architectures cannot be selected together locally because each must
run on a matching native macOS process. `--target=all` therefore selects only
one macOS process architecture and skips macOS work entirely on a non-macOS
host. The GitHub all-platform workflow uses two different macOS runners and
produces all five artifacts.

Before changing the application version, the dispatcher runs the complete
pytest suite. Use `--skip-tests` only when an equivalent external gate has
already passed. `--pre-release` is valid only with `--action=release`.
Publishing also requires an authenticated GitHub CLI and
`CAVEVIEWER_RELEASE_SIGNING_PRIVATE_KEY`.

## Post-release checklist

- Confirm the workflow used the intended branch, version, notes, and channel.
- Confirm all five user artifacts are present on the same GitHub tag.
- Confirm `src/caveviewer/version.py` contains the released version.
- Confirm every platform/channel manifest contains the expected version, URL,
  byte size, and SHA-256.
- Verify every platform's `.json.sig` files with the bundled public key.
- Confirm macOS legacy aliases still match the ARM64 manifests and signatures.
- Confirm the selected branch contains the single release metadata commit and
  has no unexpected generated files.
- Confirm the Essential Tests gate passed and inspect any separate push-triggered
  CI runs.
- For stable releases, verify the GitHub release is not marked prerelease and
  that the “latest release” link resolves to the new tag.
- Smoke-test install, launch, map import, and update handoff on each available
  platform/architecture; report any platform that was not tested directly.
