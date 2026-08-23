# Releases

This document is the canonical release checklist for CaveViewer. Script CLI
details remain in [`scripts/README.md`](../../scripts/README.md), while update
configuration and local packaging variables remain in
[`source-setup.md`](source-setup.md).

## Release governance

All published releases use pull-request and branch gates. Never publish from a
developer feature branch or directly from `main`:

1. Merge the intended source branch into protected `main` through a pull
   request after every required Essential Tests status check succeeds against
   the current base.
2. Run the protected **Prepare Release Next** workflow from `main`. Its approved
   release App fast-forwards `release/next` to that exact protected `main` tip.
   Every `publish: true` release build must run from `release/next`; this is the
   only release-producing branch. Contributors do not push this synchronization
   directly.
3. After the finalizer commits version, AppStream, and signed update metadata to
   `release/next`, merge `release/next` back into protected `main` through a
   second pull request. The metadata PR must pass its required lightweight
   release-metadata validation before merge.

The repository ruleset requires pull requests and strict required status
checks for `main`; nobody has a bypass. A branch that passed against an older
`main` must be brought up to date and validated again. `release/next` must not
accumulate a second release while metadata from the preceding release remains
unmerged.

Contributors using PyCharm should run the tracked **Preview Release** promotion
or the appropriate **Release …** platform configuration from `.run/`. These are
the supported local front ends to the repository-owned release automation and
GitHub CLI. Keep personal IDE state in
ignored `.idea/` files, authenticate locally with `gh auth login`, and do not
store tokens in a run configuration. A GitHub Actions IDE plug-in is not
required or recommended for dispatching releases; PyCharm's bundled GitHub
support may still be used for editing workflow YAML and ordinary pull-request
work.

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
`1.0.64`. Do not use preview or build suffixes such as `1.0.64-rc1` or
`1.0.64+build1`: the update checker treats nonnumeric components as an
unparseable version and will not offer that release as a newer update.
Preview status is represented by the `preview` workflow input, GitHub's
prerelease flag, and the `preview.json` channel—not by a version suffix.

GitHub workflow inputs must use the bare numeric version, not `v1.0.64`.
Artifact upload paths use that input verbatim. Local release scripts normalize
an optional leading `v`, and GitHub uses the tag `v<version>`.

- A stable publish updates `stable.json`. A newly created GitHub release is a
  normal release.
- A Preview publish updates `preview.json`. A newly created GitHub release is
  marked as a GitHub prerelease.
- Uploading to an existing tag does not change its release notes or
  GitHub prerelease/latest status.
- Stable and preview manifests are independent. Publishing one channel must
  not overwrite the other channel.
- A platform/channel manifest pair exists only after that exact package has
  been published and verified on a GitHub Release. An absent preview pair
  means that no preview is currently available for that target; do not keep
  or reconstruct a manifest whose release asset is missing.
- Every frozen package embeds its immutable `release_channel`. A stable package
  follows only `stable.json`; a preview package follows only
  `preview.json`. The package workflow sets that value from `preview`,
  including package-only validation runs.
- Versions must increase within each channel. Reissuing the same numeric
  preview version does not produce an update for an existing preview
  installation.
- Packages using the retired `prerelease` CaveViewer channel follow signed
  compatibility aliases of `preview.json`, allowing those installations to
  discover a current Preview release. Current packages and tooling continue to
  use only the `preview` channel name. Moving from Preview to Stable remains an
  explicit manual Stable install.

Published platform/channel pairs use these paths:

```text
updates/windows/<stable|preview>.json
updates/linux/x86_64/<stable|preview>.json
updates/macos/arm64/<stable|preview>.json
updates/macos/x86_64/<stable|preview>.json
```

Every published manifest has a matching `.json.sig`. macOS ARM64 publishing
also copies the signed files to the legacy aliases at
`updates/macos/<stable|preview>.json[.sig]`. Those aliases must remain
byte-for-byte identical to the ARM64 files. Preview publishing additionally
copies every platform's signed `preview.json[.sig]` pair to
`prerelease.json[.sig]` for old application versions. These are path aliases,
not a supported release-channel name for current code or release tooling.
`.gitattributes` forces every update JSON file to use LF line endings so Git
cannot rewrite signed manifest bytes during a Windows checkout or commit.

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
package metadata has the same channel before it creates GitHub assets. After
upload, it reads the GitHub Release API and verifies the exact asset name,
published HTTPS URL, byte size, and SHA-256 before it writes manifests or a
metadata commit.

The application checks architecture-specific manifests from the selected
branch and channel; it does not derive updates from GitHub's “latest release”
metadata. For a newer release it requires the signed manifest to have a
numeric dotted version, HTTPS allowed-package URL, positive integer byte size,
and complete SHA-256. After verifying the manifest signature, it probes the
package URL and offers the artifact only when that URL resolves. A missing
preview manifest, or a signed candidate whose package returns HTTP 404 or
410, is treated as no update; the technical reason is logged. The app then
verifies the artifact size and SHA-256 while downloading.

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

## Release workflows

[`all-platform-release.yml`](../../.github/workflows/all-platform-release.yml)
is the only complete four-platform build/publish workflow. For every published
release it is dispatched on `release/next`, never on `main` or a feature branch.
Package-only (`publish: false`) validation may run on another branch because it
does not create a release or commit release metadata.

### One-action Preview promotion

Use **Preview Release Promotion** when a validated feature branch is ready to
become the next Preview. The selected source branch must already be present in
protected `main`; all publication still occurs exclusively from `release/next`.

From a local checkout of the branch to release, one command starts the entire
promotion:

```bash
gh workflow run preview-release-promotion.yml \
  --ref main \
  -f source_branch="$(git branch --show-current)" \
  -f release_notes="Describe this Preview"
```

The preferred entry point is PyCharm's shared **Preview Release** run
configuration. It validates that the current feature branch is
clean and fully pushed, prompts for optional notes and an explicit confirmation,
dispatches the same workflow, resolves the exact new run, and watches it to
completion. The configuration is stored in the tracked `.run/` directory; it
contains no token, account name, personal path, or release secret.

The `gh workflow run` command above is the supported terminal fallback. The
GitHub Actions web interface exposes the same `source_branch` and
`release_notes` inputs, but contributors should normally use the shared
PyCharm configuration so its clean-tree, pushed-branch, confirmation, and exact
run-tracking checks are not skipped. When dispatching manually, run the
promotion workflow definition from `main` and explicitly name the feature
source branch.

The promotion is Preview-only and performs one strictly ordered sequence:

1. Confirm `release/next` has no release metadata still missing from `main`.
2. Confirm the selected source branch tip is already reachable from `main`.
3. Merge current `main` into `release/next` and push that exact source.
4. Choose one greater patch version from the current application version and
   all existing numeric GitHub release tags (including tags without a release).
5. Dispatch **All Platform Release** on `release/next` with `preview` and
   `publish` enabled, then wait for its complete Essential Tests and package run.
6. Report the release, source SHA, metadata commit, and compare URL. A
   maintainer opens, reviews, and merges `release/next` into `main` manually.

Every mutation follows a successful preflight or workflow. A source branch not
already present in `main` leaves both long-lived branches unchanged. A failed
package workflow remains visible in **All Platform Release**. Rerun only after
correcting the reported failure. The repository-wide promotion concurrency
group prevents two Preview promotions from overlapping.

The workflow intentionally rejects `main` and `release/next` as source-branch
inputs. It also refuses to begin while `release/next` differs from `main`; merge
the preceding release-metadata PR first. This prevents two release versions
from accumulating on the long-lived release branch.

The orchestrator needs `actions: write` to dispatch the immutable release run
and `contents: write` to synchronize `release/next`; it has no pull-request
permission. No personal access token is required, and no workflow creates or
merges a pull request.

### Direct stable and platform release procedure

Stable and individual-platform publishing use the same branch topology:

1. Merge the release candidate into `main` through its fully gated source PR.
2. From checked-out `main`, run the tracked PyCharm **Prepare Release Next**
   action. Wait for it to fast-forward protected `release/next` successfully;
   do not push `release/next` with a contributor credential.
3. Fetch and check out the prepared `release/next`, then run the tracked
   PyCharm **All Platform
   Release** or desired **Release …** platform action. The launcher selects the
   next version automatically, asks for `preview` or `stable` with Preview as
   the default, and explicitly enables publication.
4. Wait for the workflow's Essential Tests and every package validation to
   succeed for the immutable release source.
5. After successful publication, manually open, review, and merge the
   `release/next` to `main` metadata PR after its required checks pass. Do not
   start another release first.

### Manual metadata reconciliation

No workflow creates, approves, or merges the final metadata pull request. After
publication succeeds, a maintainer performs this gate explicitly:

```bash
git fetch origin main release/next
gh pr create \
  --base main \
  --head release/next \
  --title "Reconcile release metadata" \
  --body "Merge the published release metadata from release/next."
gh pr checks --watch "$(gh pr view release/next --json number --jq .number)"
```

Review the diff and confirm it contains only the expected version, AppStream,
changelog, and signed update-manifest metadata. Merge the PR through the normal
protected-branch control only after every required check succeeds. The next
release starts with **Prepare Release Next**, which fast-forwards
`release/next` to the newly reconciled `main` tip. PyCharm's bundled pull-request
support or the GitHub website may be used for this human review; no workflow
plug-in or stored IDE token is required.

If a pull request for `release/next` is already open, review and use that PR
instead of opening a duplicate.

For package-only validation, dispatch through GitHub and clear `publish`, or use
the package-smoke workflow. GitHub retains workflow artifacts without creating
a GitHub Release or changing update metadata. PyCharm actions named **Release**
always publish; they are not build-only shortcuts.

### Release branch hygiene

Treat a "publish: true" run as a durable release operation, not a build trial:
it creates or updates the GitHub Release and writes the version, AppStream, and
signed update-manifest metadata. Use "publish: false" until the candidate is
ready to publish.

Do not publish successive versions before reconciling `release/next`. After
publishing, merge its release-metadata pull request into `main`, then synchronize
`release/next` from that updated `main` before publishing again. The finalizer
rejects a release target whose version, AppStream, or update metadata differs
from `origin/main`; this prevents multiple unmerged AppStream release entries
from accumulating in one pull request.

If publication succeeds but reconciliation is interrupted, stop the release
queue: do not select or publish a newer version. Fetch both branches and inspect
the published workflow summary, GitHub Release, and `origin/release/next`:

- If the finalizer's metadata commit is on `release/next`, open or resume the
  manual metadata PR above and merge it after required checks pass.
- If assets exist but no metadata commit reached `release/next`, the release is
  not advertised to updaters. Preserve the immutable source and inspect the
  failed finalizer. After correcting the failure, manually dispatch the same
  release workflow with the same numeric version, channel, platform set, and
  `publish: true`; do not let the automatic next-version launcher skip to a new
  version. The finalizer must verify the existing remote assets and create the
  one metadata commit before reconciliation.
- If metadata has reached `main` but `release/next` is behind it, run **Prepare
  Release Next**. Do not repair either protected branch with a force push.

Resume normal version selection only when the metadata PR is merged and
**Prepare Release Next** reports that `release/next` matches protected `main`.

An intentionally partial platform publish may be resumed with the same version:
the finalizer permits that only when the branch has exactly one new AppStream
release record and it matches the requested version. It rejects a different
version or multiple unmerged release records.

If a release is withdrawn, remove or revert the matching source metadata as
well as the GitHub Release and tag. Deleting a GitHub Release or tag alone does
not change the tracked release metadata.

By default, the workflow runs the shared Essential Tests gate once, then fans
out four package jobs from the same immutable source revision:

```text
                    ┌─ Windows ────────┐
Essential Tests ────├─ Linux x86_64 ───┼─ Finalize Release
                    ├─ macOS ARM64 ────┤
                    └─ macOS x86_64 ───┘
```

Each called platform workflow skips its duplicate internal gate because the
all-platform caller just ran the same Essential Tests for the immutable source.
A platform workflow dispatched on its own always runs Essential Tests before
packaging. The native Intel release job also retains its platform-specific
source suite and CLI checks.

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
or update the GitHub release once, and verify every uploaded asset against the
GitHub Release API. Only after the remote URL, size, and SHA-256 match does it
write and sign every requested manifest, update
`src/caveviewer/version.py`, and push one metadata commit. A failed upload or
remote verification therefore leaves the previously committed manifests in
place.

Every package checks out the workflow's starting commit rather than a moving
branch head. Before publishing, the finalizer verifies that the selected branch
still points to that commit. A concurrent source push therefore fails the
release safely instead of mixing artifacts and metadata from different source
revisions. Finalizers from complete and individual platform workflows share a
branch-level concurrency lock.

The `production-release` environment secret
`CAVEVIEWER_RELEASE_SIGNING_PRIVATE_KEY` must contain the Ed25519 private key
used for update manifests. Each publisher's finalizer call explicitly inherits
secrets across GitHub's reusable-workflow boundary, but only the called
finalizer attaches the approved environment and resolves those values. Package,
test, and artifact-only jobs neither inherit nor receive release secrets.
Before downloading release artifacts or publishing anything, the finalizer
derives the private key's Ed25519 public key and requires an exact match with
`src/caveviewer/resources/release_signing_public_key.pem`. A malformed,
wrong-type, or mismatched key fails without creating a release or metadata.

The GitHub release workflows do not currently offer an Authenticode signing
path. Selecting `publish` for Windows automatically builds the same named EXE
on `windows-latest`, marks its package metadata `unsigned-community`, and
permits the finalizer to publish it. The finalizer verifies the installer size
and SHA-256 locally and again against GitHub's uploaded asset metadata, then
signs the Windows update manifest with
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
and call `scripts/macos/smoke_dmg.sh`. ARM64 validation runs automatically for
relevant pull requests, pushes, and its weekly schedule. Intel validation is
manual-only and must be dispatched against the branch that needs x86_64
coverage. The validator checks package metadata and digest, mounts the DMG
read-only, verifies bundle identity and version, checks every bundled Mach-O
file for the requested architecture and runner-local library references, and
exercises a controlled packaged CLI error path. The Intel smoke workflow also
runs the complete pytest suite and source CLI checks on `macos-15-intel` before
building. The Intel release workflow always runs the same native source checks.

Dispatch Intel package validation from the Actions tab or with GitHub CLI:

```bash
gh workflow run macos-x86_64-package-smoke.yml --ref <branch>
```

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

## Existing tags and channel separation

The release finalizer creates the tag/release only when it does not already
exist. When it does exist, it uploads with `--clobber`; it does not change the
release notes or GitHub prerelease/latest status.

Stable and Preview may not share a tag or numeric version. The finalizer rejects
an existing tag whose GitHub prerelease state differs from the requested
channel. Publish Stable with a new version greater than every advertised
Preview version; do not edit a Preview tag into a Stable release.

## Individual and resumed releases

The four platform workflows remain manually dispatchable. A direct dispatch
publishes by default and calls the same single-writer finalizer for that
platform. It leaves generated metadata on `release/next` for a manual PR into
`main`. Clearing `publish` keeps it artifact-only. Any direct publish
must still use `release/next`; the launcher and finalizer both reject a feature
branch or `main`. Use the same
`release/next` commit, version, release notes, `publish`, and `preview` values
when resuming an intentionally partial release.

An all-platform build failure does not publish any platform or manifest because
the finalizer requires all four package jobs to succeed. Inspect the retained
workflow artifacts and correct the failure before rerunning All Platform
Release. If upload or post-upload verification fails, GitHub may contain an
unadvertised partial release, but the previously committed update manifest
remains authoritative. Inspect or remove the partial assets before rerunning;
do not manually point a manifest at them.

## Local dispatcher

Use [`scripts/release.sh`](../../scripts/release.sh) for local build and package
operations:

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
already passed. `--preview` is valid with `build`, `package`, and
`release`; it selects the preview metadata embedded in every resulting
package, while `release` also marks the GitHub release as a preview.
Publishing also requires an authenticated GitHub CLI and a local
`CAVEVIEWER_RELEASE_SIGNING_PRIVATE_KEY`; local recovery publication cannot
read GitHub environment secrets.
Local publishing is an exceptional recovery path, not the normal contributor
workflow. If it is required, check out synchronized `release/next`, publish
there, push its metadata commit, and merge that metadata into `main` through the
same required-check PR. Never publish locally from `main` or a feature branch.

## Post-release checklist

- Confirm the published workflow used `release/next` and the intended version,
  notes, and channel.
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
- For stable releases, verify the GitHub release is not marked as a GitHub prerelease and
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
