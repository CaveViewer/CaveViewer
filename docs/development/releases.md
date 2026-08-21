# Releases

This document is the canonical release checklist for CaveViewer. Script CLI
details remain in [`scripts/README.md`](../../scripts/README.md), while update
configuration and local packaging variables remain in
[`source-setup.md`](source-setup.md).

## Release matrix

CaveViewer publishes four user-installable artifacts from four GitHub workflows:

| Target | Workflow | Runner | User artifact |
|---|---|---|---|
| `windows` | `Windows Release` | `windows-latest` | `CaveViewer-<version>-windows.exe` |
| `linux-x86_64` | `Linux x86_64 Release` | `ubuntu-latest` | `CaveViewer-<version>-x86_64.AppImage` |
| `macos-arm64` | `macOS ARM64 Release` | `macos-15` | `CaveViewer-<version>-macos-arm64.dmg` |
| `macos-x86_64` | `macOS x86_64 Release` | `macos-15-intel` | `CaveViewer-<version>-macos-x86_64.dmg` |

The Windows release asset keeps the single familiar
`CaveViewer-<version>-windows.exe` name. The GitHub release workflows currently
publish it only as an explicit unsigned community installer, so Windows warnings
are expected. It embeds a PyInstaller one-folder
CaveViewer payload, installs per-user under
`%LOCALAPPDATA%\Programs\CaveViewer`, and keeps user state at the compatible
`%USERPROFILE%\.caveviewer` location. The installer adds a versioned payload
directory only after copying a complete frozen payload, verifies a controlled
non-GPU executable path, updates shortcuts, and can wait for an updater parent
with `--update --wait-pid <pid> --expected-version <version>`. After controlled
payload verification it records the current per-user payload/executable
provenance and relaunches the new application. The asset name is versioned for
release integrity, while the embedded installer entrypoint is
`CaveViewerSetup.exe`.

`Windows Package Smoke` exercises the unsigned test-only mechanical contract on a
disposable Windows runner, including paths with spaces, Unicode, apostrophes,
and ampersands, isolated noninteractive installation, and the update wait
handoff. A community publishing `Windows Release` uses the same smoke coverage
on a GitHub-hosted Windows runner but requires the exact `unsigned-community`
metadata policy instead.

The legacy Windows `launch.bat`/`setup.ps1` source helpers remain available for
developers and migration support only; they are not included in the new
release artifact. The DMGs and AppImages are bundled applications.

GitHub automatically provides its own source-code ZIP and tarball for each tag.
`scripts/common/package_source.sh` can create
`CaveViewer-<version>-source.tar.gz` locally, but the current release workflows
do not upload that tarball as a release asset.

## Channels and update paths

Release versions must contain only dot-separated decimal integers, such as
`1.0.64`. Do not use prerelease or build suffixes such as `1.0.64-rc1` or
`1.0.64+build1`: the update checker treats nonnumeric components as an
unparseable version and will not offer that release as a newer update.
Prerelease status is represented by the `pre_release` workflow input, the
GitHub release flag, and the `prerelease.json` channel—not by a version suffix.

GitHub workflow inputs must use the bare numeric version, not `v1.0.64`.
Artifact upload paths use that input verbatim. Local release scripts normalize
an optional leading `v`, and GitHub uses the tag `v<version>`.

- A stable publish updates `stable.json`. A newly created GitHub release is a
  normal release.
- A prerelease publish updates `prerelease.json`. A newly created GitHub
  release is marked as a prerelease.
- Uploading to an existing tag does not change its release notes or
  prerelease/latest status.
- Stable and prerelease manifests are independent. Publishing one channel must
  not overwrite the other channel.
- Every frozen package embeds its immutable `release_channel`. A stable package
  follows only `stable.json`; a prerelease package follows only
  `prerelease.json`. The package workflow sets that value from `pre_release`,
  including package-only validation runs.
- Versions must increase within each channel. Reissuing the same numeric
  prerelease version does not produce an update for an existing prerelease
  installation.
- Existing prerelease installations made before this metadata was embedded
  need one manual installation of the first channel-aware prerelease. They
  currently follow `stable.json`; after that one install, later prerelease
  updates stay automatic within `prerelease.json`. Moving from prerelease to
  stable remains an explicit manual stable install.

Published manifest paths are:

```text
updates/windows/<stable|prerelease>.json
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

`scripts/write_update_manifest.py` is the sole manifest serializer. Before a
manifest can be signed, it canonicalizes the numeric version, reads the built
artifact, and writes its positive byte size and lowercase 64-character
SHA-256. The payload URL must be HTTPS and match the selected platform package
type (Windows EXE, Linux AppImage, or macOS DMG). A Windows EXE manifest also
contains `install_channel: windows_installer` and the exact
`authenticode_certificate_subject` verified from package metadata; the client
requires both before it can offer automatic install/restart. Already-published
Windows ZIP manifests remain valid during migration, and retain their
ZIP-specific alias keys. Platform shell wrappers only
choose the manifest path and delegate to this writer; do not add another
heredoc-based JSON serializer. Its canonical signed representation uses
lexicographic JSON key order, two-space indentation, and a final LF newline.
Release notes may contain quotes, backslashes, Unicode, and newlines.

New manifests also serialize `release_channel`, which is covered by their
Ed25519 signature and must equal the package's selected update channel before
the client offers an artifact. The client temporarily accepts older signed
manifests without that field as a documented compatibility window; writers
always include it. The finalizer independently verifies every platform's
package metadata has the same channel before it creates GitHub assets, tags,
manifests, or metadata commits.

The application checks architecture-specific manifests from the selected
branch and channel; it does not derive updates from GitHub's “latest release”
metadata. For a newer release it requires the signed manifest to have a
numeric dotted version, HTTPS allowed-package URL, positive integer byte size,
and complete SHA-256 before offering its artifact. It then verifies the
artifact size and SHA-256 while downloading.

Linux packages install the stable application ID
`io.github.caveviewer.caveviewer`. The desktop filename, AppStream ID,
hicolor icon basename, Wayland app ID, and X11 `StartupWMClass` must remain
identical, and the desktop file keeps `StartupNotify=true` for compositor
launch feedback. The desktop file advertises `model/gltf-binary` and
`model/obj` with an `Exec ... %f` field so GNOME file managers can offer
CaveViewer for direct `.glb` and `.obj` launches; AppStream metadata must
provide the same media types. Packaging renders `packaging/linux/*.desktop.in`
rather than maintaining a second inline desktop entry. Release version updates
prepend the matching AppStream release entry through
`scripts/common/version.sh`. The AppImage runtime integration copies the
desktop file, hicolor icons, and metainfo file into the user's XDG data home
without changing the user's default MIME associations. Set
`CAVEVIEWER_APPRUN_INSTALL_ONLY=1` when launching an AppImage from a terminal to
smoke-test only this desktop integration path without starting the GUI. Set
`CAVEVIEWER_APPRUN_UNINSTALL=1` to remove the per-user desktop file, AppStream
metadata, and hicolor icons without removing maps, settings, caches, or
downloaded update packages.

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
6. Turn `pre_release` on when this is a prerelease package: it embeds the
   prerelease subscription, updates `prerelease.json` when publishing, and
   marks a newly created GitHub release as a prerelease.
7. Turn `reuse_pr_validation` on only when the selected source has already
   passed its PR validation and no application, packaging, dependency, test, or
   workflow change has been made since. This skips the duplicate source test
   suites, not package creation or package validation. Documentation, release
   notes, and other release metadata changes alone do not require the source
   suites to run again.

By default, the workflow runs the shared Essential Tests gate once, then fans
out four package jobs from the same immutable source revision:

```text
                    ┌─ Windows ────────┐
Essential Tests ────├─ Linux x86_64 ───┼─ Finalize Release
                    ├─ macOS ARM64 ────┤
                    └─ macOS x86_64 ───┘
```

Each called platform workflow skips its duplicate internal gate. A platform
workflow dispatched on its own still runs Essential Tests before packaging.
When `reuse_pr_validation` is selected, the shared gate and the duplicate
native Intel source suite are skipped; the four platform packages and their
release-time package checks still run. Use that option only for the already
validated source described above.

Normal code, dependency, packaging, test, and workflow pushes to `main` or
`release/**` also trigger `.github/workflows/tests.yml`; those branch-CI runs
are separate from the single gate inside All Platform Release. Release-only
metadata commits touch the changelog, version, AppStream release metadata, or
signed update manifests, so they do not start a duplicate broad test or
package-smoke run.

When that generated metadata is proposed back to protected `main` in a pull
request, Essential Tests keeps the required check names but does not rerun the
source suites. It validates the release-only diff, version/AppStream entry,
update-manifest schema, and Ed25519 signatures instead. Any application,
dependency, test, workflow, or other packaging change falls back to the full
source suite. This lets a release-metadata pull request merge without treating
existing package artifacts as new application code. A malformed metadata diff
fails that lightweight validation rather than silently bypassing checks.

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

The GitHub release workflows do not currently offer an Authenticode signing
path. Selecting `publish` for Windows automatically builds the same named EXE
on `windows-latest`, marks its package metadata `unsigned-community`, and
permits the finalizer to publish it. The finalizer still verifies the installer
size and SHA-256, then signs the Windows update manifest with
`CAVEVIEWER_RELEASE_SIGNING_PRIVATE_KEY`. Installed CaveViewer applications
verify that manifest signature and require an explicit `Install and restart`
click before launching the downloaded installer. This does not remove Windows
SmartScreen or unknown-publisher warnings; users must accept those warnings
until the project obtains Authenticode signing.

Hosted package-only runs may create an explicitly `unsigned-test-only` artifact
for mechanical validation, but the finalizer and local publisher reject that
metadata so it cannot be published. The finalizer accepts no other unsigned
Windows status.

## macOS package validation

The ARM64 and Intel package-smoke workflows run on native GitHub-hosted runners
and call `scripts/macos/smoke_dmg.sh`. The validator checks package metadata and
digest, mounts the DMG read-only, verifies bundle identity and version, checks
every bundled Mach-O file for the requested architecture and runner-local
library references, and exercises a controlled packaged CLI error path. The
Intel smoke workflow also runs the complete pytest suite and source CLI
checks on `macos-15-intel` before building. The Intel release workflow does the
same by default, and skips only those duplicate source checks when the release
uses `reuse_pr_validation`.

Run the same package validation locally after creating a native DMG:

```bash
./scripts/macos/smoke_dmg.sh --arch=arm64 --version=1.0.64
```

Use `--arch=x86_64` from a native Intel process. The script rejects a process
whose architecture does not match the package target.

## GitHub Pages

GitHub Pages deployment is independent from application releases. The
[`Pages`](../../.github/workflows/pages.yml) workflow packages only `docs/` and
deploys it through the `github-pages` environment. It runs after changes to
`docs/**` or its own workflow reach `main`, and it can also be dispatched
manually from `main`. Release workflows do not call or depend on it.

Repository Pages settings must use **GitHub Actions** as the publishing source,
not the legacy `main` branch `/docs` source. Keep the `github-pages` environment
restricted to `main` so a manual dispatch from another branch cannot publish.

## Existing tags and prerelease promotion

The release finalizer creates the tag/release only when it does not already
exist. When it does exist, it uploads with `--clobber`; it does not change the
release notes or prerelease/latest status.

This matters when the same version is first published as a prerelease and later
promoted to stable. Uploading stable assets and manifests is not enough. After
all stable platform jobs succeed, edit that tag on GitHub, clear the prerelease
flag, publish it as the normal release, and verify that
[`releases/latest`](https://github.com/CaveViewer/CaveViewer/releases/latest)
resolves to it.

## Individual and resumed releases

The four platform workflows remain manually dispatchable. A direct workflow
runs its package job and, when `publish` is enabled, calls the same single-writer
finalizer for that platform. Use the same source branch, version, release notes,
`publish`, and `pre_release` values when resuming an intentionally partial
release.

An all-platform build failure does not publish any platform or manifest because
the finalizer requires all four package jobs to succeed. Inspect the retained
workflow artifacts and correct the failure before rerunning All Platform
Release. If a finalizer fails after publishing external release state, do not
rerun it blindly against protected `main`: first reconcile that state through
an auditable metadata pull request.

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

Targets are `windows`, `linux-x86_64`, `macos-arm64`, `macos-x86_64`, and
`all`. A comma-separated target list is also accepted.
Unlike the GitHub workflows, the local dispatcher accepts an optional leading
`v` in the version.

Both macOS architectures cannot be selected together locally because each must
run on a matching native macOS process. `--target=all` therefore selects only
one macOS process architecture and skips macOS work entirely on a non-macOS
host. The GitHub all-platform workflow uses two different macOS runners and
produces all four artifacts.

Before changing the application version, the dispatcher runs the complete
pytest suite. Use `--skip-tests` only when an equivalent external gate has
already passed. `--pre-release` is valid with `build`, `package`, and
`release`; it selects the prerelease metadata embedded in every resulting
package, while `release` also marks the GitHub release as a prerelease.
Publishing also requires an authenticated GitHub CLI and
`CAVEVIEWER_RELEASE_SIGNING_PRIVATE_KEY`.

## Post-release checklist

- Confirm the workflow used the intended branch, version, notes, and channel.
- Confirm all four user artifacts are present on the same GitHub tag.
- Confirm `src/caveviewer/version.py` contains the released version.
- Confirm every platform/channel manifest contains the expected version, URL,
  byte size, and SHA-256.
- Verify every platform's `.json.sig` files with the bundled public key.
- Confirm macOS legacy aliases still match the ARM64 manifests and signatures.
- Confirm the selected branch contains the single release metadata commit and
  has no unexpected generated files.
- Confirm either the full Essential Tests gate passed or the pull request was
  classified as release-only metadata and its lightweight metadata validation
  passed. Inspect any separate push-triggered CI runs.
- Confirm both native macOS package-smoke workflows passed for macOS packaging,
  dependency, or release-script changes.
- For stable releases, verify the GitHub release is not marked prerelease and
  that the “latest release” link resolves to the new tag.
- Smoke-test install, launch, map import, and background update download on
  each available platform/architecture. Confirm macOS, Linux, and Windows ZIP
  migration packages remain reveal-only. On a signed Windows installer build,
  also exercise `Install and restart`: validate the publisher/timestamp,
  update wait/version contract, controlled payload verification, and relaunch.
  On an unsigned community Windows build, confirm the manifest reports
  `unsigned-community`, the package hash is accepted, the Windows warning is
  expected, and the same update wait/version and relaunch contract succeeds.
  Report any platform that was not tested directly.
- Validate the rendered Linux desktop file with `desktop-file-validate` and the
  metainfo file with `appstreamcli validate --no-net --pedantic`.
- Smoke-test AppImage desktop integration with
  `CAVEVIEWER_APPRUN_INSTALL_ONLY=1 ./CaveViewer-<version>-x86_64.AppImage` and
  confirm the printed desktop, metainfo, and hicolor icon paths.
- Smoke-test AppImage desktop integration removal with
  `CAVEVIEWER_APPRUN_UNINSTALL=1 ./CaveViewer-<version>-x86_64.AppImage` and
  confirm the printed paths were removed.
- Smoke-test the Linux AppImage on GNOME Wayland and Xorg. On GNOME Wayland
  with `DISPLAY` available, confirm the shared Linux `auto` backend starts the
  viewer through X11/XWayland so source/debug and AppImage launches have the
  same titlebar and resize behavior. Also confirm launcher/icon grouping,
  portal selection/reveal, fractional scaling, fullscreen transitions, and
  normal input controls.
