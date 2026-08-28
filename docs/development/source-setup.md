# CaveViewer Development From Source

This guide is the operational reference for obtaining and running the source,
IDE/shell setup, tests, variables, storage locations, and troubleshooting.
Cross-layer contracts live in [Architecture](architecture.md), while setting
ownership and precedence live in
[Runtime configuration](runtime-configuration.md).

Scope:

- This document is intentionally focused on source-based development and local runs.

Contributor workflow, architecture, repository layout, coding, testing, and
AI-assistant guidance are indexed in the
[development documentation](README.md). See
[`CONTRIBUTING.md`](../../CONTRIBUTING.md) before preparing a change.
The canonical platform release sequence and verification checklist are in
[`releases.md`](releases.md).

## Get Source Files

You can start in either of these ways:

- Clone the repository with Git (recommended for contributors).
- Download source files from GitHub and unpack them locally.

The optional local source archive format is:

- `CaveViewer-<version>-source.tar.gz`

This format is produced by `scripts/common/package_source.sh`. Current GitHub
release workflows do not upload it; GitHub supplies its standard tag source
archives automatically.

Release packages should include:

- `LICENSE`
- `THIRD_PARTY_NOTICES.md`

The application About text should identify CaveViewer as licensed under the GNU Affero General Public License version 3.0 only.

## Requirements

- Git
- Python 3.12

You also need to run a typical workstation setup with C++ and other compilers if you desired to compile from source.

Ubuntu should work out of the box. Fedora 44 is special, so you have to install additional packages
```bash
sudo dnf install gcc gcc-c++ make python3.12 python3.12-devel \
    python3.12-tkinter mesa-libGL-devel mesa-libEGL-devel libX11-devel
```

## Clone the Repository

```bash
git clone git@github.com:CaveViewer/CaveViewer.git
cd CaveViewer
```

Optional: check out the latest version tag.

```bash
git fetch --tags
latest=$(git tag -l "v*" --sort=-version:refname | head -n 1)
git checkout "$latest"
```

## Shared PyCharm workflows

The shared **CaveViewer** run configuration uses PyCharm's available project
interpreter only to bootstrap the repository. It installs pinned `uv` 0.12.5
when needed; `uv` downloads a managed Python 3.12 runtime, creates
`.venv-dev`, and seeds pip on Linux, macOS, or Windows. The launcher then
restarts itself with that repository-local interpreter and installs
`requirements.txt` before starting the application.

This makes the first run on a fresh checkout self-bootstrapping when the
machine has network access and PyCharm can start the bootstrap interpreter.
Subsequent runs reuse `.venv-dev` and leave already-satisfied packages
unchanged. The tracked `.python-version` also pins compatible tools to Python
3.12. Runtime provisioning and dependency setup must succeed before the
application is launched.

PyCharm contributors should use the versioned **Release Actions** run
configurations under `.run/`. The normal choices are **Create Preview Release**
and **Create Stable Release**. Each requires a clean checked-out `main` whose
HEAD exactly matches `origin/main`, displays the exact next version, asks for
confirmation, publishes every platform, and reports the metadata pull request
that it creates or reuses with the developer's existing `gh` authentication.
That pull request still requires human review and a manual merge.

**Prepare Release Next**, **All Platform Release**, and individual-platform
actions are grouped under **Release Actions - Advanced Recovery**. They are for
controlled recovery, not the normal release process. Internally all publication
and metadata still use `release/next`; the promotion workflow handles that
branch. After the workflow succeeds, the local launcher creates, but never
merges, the metadata PR.

The tracked configurations contain no credentials or machine-specific paths.
Keep personal settings, interpreter choices, window layout, and local
environment variables under the ignored `.idea/` directory. Authenticate the
GitHub CLI once on each workstation with `gh auth login`; never add `GH_TOKEN`,
`GITHUB_TOKEN`, signing keys, or passwords to `.run/` or `.idea` run
configurations.

No third-party GitHub Actions PyCharm plug-in is needed for release execution.
The shared configuration and `gh` CLI are preferred because their behavior is
reviewed and tested with the repository. PyCharm's bundled GitHub support may
still provide workflow-YAML completion and normal pull-request integration.
See [releases.md](releases.md) for branch gating and the full release sequence.

## Optional: Make This Local Clone Read-Only (No Push)

If you only want to pull and run from source, you can disable push behavior in this clone.

```bash
# 1) Disable default pushes from this repo
git config --local push.default nothing

# 2) Set an intentionally invalid push URL for origin
git remote set-url --push origin DISABLED

# 3) Verify read-only push setup
git config --local --get push.default
git remote -v
```

Expected result:

- `push.default` shows `nothing`
- `origin (push)` shows `DISABLED`

With this setup, normal pull/fetch operations continue to work, but push attempts fail immediately.

## macOS and Linux: Run From Source

Use the project bootstrap script:

```bash
./scripts/dev/install.sh
```

What it does:

- Creates a development virtual environment at `.venv-dev` (or `CAVEVIEWER_DEV_VENV` if set)
- Uses Python 3.12 from `CAVEVIEWER_PYTHON`, `python3.12`, or `python3`
- Installs dependencies from `requirements.txt`
- Installs CaveViewer in editable mode from `src/`
- Generates `run_caveviewer.sh`

Run the app:

```bash
./run_caveviewer.sh
```

Alternatively:

```bash
# If you activated the virtual environment, run the 'source' line below
source .venv-dev/bin/activate
.venv-dev/bin/python -m caveviewer
```

## Windows: Run From Source

Option A (recommended for technical users): manual venv flow.

```powershell
py -3.12 -m venv .venv-dev
.\.venv-dev\Scripts\python -m pip install --upgrade pip
.\.venv-dev\Scripts\python -m pip install -r requirements.txt
.\.venv-dev\Scripts\python -m pip install --no-deps -e .
.\.venv-dev\Scripts\python -m caveviewer
```

Option B (guided setup script in this repo):

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\windows\setup.ps1
```

Notes:

- `scripts/windows/setup.ps1` accepts only a real 64-bit Python 3.12 interpreter, creates CaveViewer's user-owned virtual environment below `%LOCALAPPDATA%\CaveViewer\runtime`, and installs the local source tree into that environment.
- `scripts/windows/launch.bat` is a launcher for the setup script.
- Setup does not rely on a later PATH refresh or change system Python, firewall rules, or administrator-owned locations. Its retained setup and launch logs are under `%LOCALAPPDATA%\CaveViewer\logs`.
- These source-only helpers are not Windows release installers. End users
  should install the signed `CaveViewer-<version>-windows.exe` release asset.

## Run Automated Tests

Install the development-only test tools after the runtime dependencies:

```bash
.venv-dev/bin/python -m pip install -r requirements-dev.txt
.venv-dev/bin/python -m pytest
```

On Windows, use `.venv-dev\Scripts\python` in place of `.venv-dev/bin/python`.

The suite isolates the home/preferences directory, blocks uncontrolled network
connections, and uses temporary directories for all generated files. GitHub runs
syntax/import sanity checks, the unit suite on Linux, Windows, and macOS, CLI
smoke checks, and a Linux coverage/desktop-metadata gate for pull requests and
pushes to `main` or `release/**`. The same gate runs before GitHub release
builds. `All Platform Release` runs it once for the whole parallel package
fan-out; a directly dispatched platform workflow runs its own gate. Direct
`scripts/release.sh` runs also execute the complete pytest suite before changing
the application version or creating artifacts. It uses
`.venv-dev` when available, then falls back to
`python3.12`/`python3`/`python`; set `CAVEVIEWER_TEST_PYTHON=/path/to/python` to
select another prepared Python 3.12 interpreter. The interpreter must have
`requirements.txt` and `requirements-dev.txt` installed.

GitHub runs separate Windows, Linux, and macOS ARM64 package-smoke workflows for
pull requests and pushes to `main` or `release/**` when packaging, release
scripts, runtime source, or dependency files change. They also run weekly and
can be dispatched manually. The macOS Intel package-smoke workflow is
manual-only; dispatch it from the Actions tab on the branch that needs native
x86_64 validation. The Windows workflow builds an unsigned test-only EXE on a
disposable runner and validates native installer paths, isolated install
verification, and update handoff; publishing reruns the same smoke on the
protected signer and requires the real signature. The Linux workflow builds the
x86_64 AppImage
through the Docker release path, validates AppImage desktop install/uninstall
behavior in a temporary home directory, and validates the installed
desktop/AppStream metadata. Both macOS workflows build a native DMG through the
release path and use the same validator for package metadata, the actual DMG
hash and size, bundle identity and version, bundled Mach-O architectures,
runner-local library references, support documents, and packaged CLI behavior.
The Intel workflow also runs the complete test suite and source CLI smoke checks
on `macos-15-intel`. These workflows upload short-lived workflow artifacts for
inspection; they do not publish releases or write repository contents.

GitHub platform jobs pass `--skip-tests` because the application source has
already passed an essential test gate with coverage. Individually dispatched
platform workflows provide that gate themselves; `All Platform Release`
provides it once before calling every platform workflow. The gate precedes the
controlled build-time version change. Every platform checks out the same source
commit and packages independently; after all packages succeed, one finalizer
updates the version and signed manifests in a single branch commit. Do not use
`--skip-tests` for an ordinary local release unless an equivalent external gate
has completed successfully. Tests and development dependencies are not included
in release archives.

## Map Library Source Overrides

By default, the built-in map library reads release assets from:

- Repository: `CaveViewer/CaveViewer`
- Release tag: `sample-data`
- Catalog asset: `caveviewer-map-library.v1.json`

For local development, you can point the map library at a different source
before launching the program. These settings are environment variables only;
they are not exposed in the app UI.

The initial production Map Library source is a GitHub release adapter. The
Map Library itself consumes source-neutral catalog refreshes, so another
approved map source can later be added without changing splash rows or map
workflow. The release keeps each map as a `.zip` asset. When the release also includes
`caveviewer-map-library.v1.json`, that manifest controls the available map
list, row titles, ordering, optional stable folder names, fallback sizes, and
optional SHA-256 hashes. CaveViewer joins those manifest entries to the matching
GitHub release assets to get current download URLs and asset sizes. If the
catalog asset is absent, it keeps friendly bundled metadata only for archives
that are actually attached to the current release and infers titles for other
attached `.zip` assets. This lets a newly uploaded map appear before a full
catalog manifest is published without falsely showing a map that has been
removed from the release.

An optional `cave_metadata_id` joins a map entry to the bundled offline cave
metadata catalog. It is descriptive only and does not change map download,
opening, caching, or source authority. See [cave-metadata.md](cave-metadata.md)
for its schema and conservative fallback matching rules.

Catalog v1 shape:

```json
{
  "version": 1,
  "maps": [
    {
      "id": "devils-eye",
      "title": "Devils Eye",
      "asset": "Devils.Eye.3D.Map.zip",
      "sort": 30,
      "folder": "Devils Eye",
      "cave_metadata_id": "us-fl-devils-spring-system",
      "size_bytes": 91226112,
      "sha256": "optional 64-character lowercase hex digest"
    }
  ]
}
```

`id`, `title`, and `asset` are required. `folder`, `cave_metadata_id`,
`size_bytes`, `sort`, and `sha256` are optional. The `asset` value must match
the corresponding release zip filename exactly.

Precedence:

1. `CAVEVIEWER_MAP_LIBRARY_API_URL` uses a full GitHub release API URL directly.
2. Otherwise, CaveViewer builds the GitHub release API URL from `CAVEVIEWER_MAP_LIBRARY_REPO` and `CAVEVIEWER_MAP_LIBRARY_RELEASE_TAG`.
3. `CAVEVIEWER_MAP_LIBRARY_CATALOG_ASSET_NAME` overrides the manifest asset name.
4. If none are set, the defaults above are used.

The old `CAVEVIEWER_SAMPLE_MAPS_API_URL`, `CAVEVIEWER_SAMPLE_MAPS_REPO`, and
`CAVEVIEWER_SAMPLE_DATA_TAG` names remain supported as lower-priority aliases
for existing development environments.

macOS/Linux example:

```bash
CAVEVIEWER_MAP_LIBRARY_REPO="MyOrg/MyMaps" \
CAVEVIEWER_MAP_LIBRARY_RELEASE_TAG="public-maps" \
./run_caveviewer.sh
```

Windows PowerShell example:

```powershell
$env:CAVEVIEWER_MAP_LIBRARY_REPO = "MyOrg/MyMaps"
$env:CAVEVIEWER_MAP_LIBRARY_RELEASE_TAG = "public-maps"
.\.venv-dev\Scripts\python -m caveviewer
```

Advanced direct API override:

```bash
CAVEVIEWER_MAP_LIBRARY_API_URL="https://api.github.com/repos/MyOrg/MyMaps/releases/tags/public-maps" \
./run_caveviewer.sh
```

The API response must be compatible with GitHub's release API shape, including
an `assets` list with asset `name`, `browser_download_url`, and `size` fields.
CaveViewer caches the last successful remote catalog under its application
cache root and falls back to that cache, then to the bundled catalog, when
GitHub cannot be reached. Such a fallback is deliberately non-authoritative:
it cannot make a local map appear removed.

## Updating Your Local Source Environment

When the repository changes:

```bash
git pull --ff-only
```

Then refresh dependencies in your active dev venv:

```bash
.venv-dev/bin/python -m pip install -r requirements.txt
```

On Windows (PowerShell):

```powershell
.\.venv-dev\Scripts\python -m pip install -r requirements.txt
```

## Troubleshooting

- Python 3.12 not found (macOS/Linux): install Python 3.12 or set
  `CAVEVIEWER_PYTHON=/path/to/python3.12`, then rerun setup.
- Broken `.venv-dev`: remove it and rerun `./scripts/dev/install.sh`.
- Windows PowerShell policy blocks setup script: run with `-ExecutionPolicy Bypass` as shown above.

### Rendering Troubleshooting

Rendering/import strategy, low-memory tuning, GPU-driver troubleshooting, and
`caveviewer-chunker` cache-compilation options are documented in
[`rendering.md`](rendering.md).

---

## Environment Variables

For ownership, precedence, and typed transport, see
[Runtime configuration](runtime-configuration.md). The tables below remain the
operational reference for names and accepted values.

All variables are optional. Set them in your shell before launching or prefix them inline:

```bash
CAVEVIEWER_LOG_LEVEL=DEBUG ./run_caveviewer.sh
```

### Application runtime reference

The following table is generated from
`core.preferences.runtime_settings.RUNTIME_SETTING_SPECS`. Do not edit its
contents by hand; the unit test keeps the marked block synchronized with the
registry. Packaging and development-shell variables remain documented in the
sections below because they are deliberately excluded from the application
runtime snapshot.

<!-- BEGIN RUNTIME_SETTINGS_TABLE -->
| Variable | Category | Default | Description |
| --- | --- | --- | --- |
| `CAVEVIEWER_MEMORY_UTILIZATION_TARGET` | persisted preference | saved preference or platform default | Target percent of available RAM for loaded chunks. |
| `CAVEVIEWER_GPU_MEMORY_UTILIZATION_TARGET` | persisted preference | saved preference or platform default | Target percent of GPU memory for texture and geometry residency. |
| `CAVEVIEWER_GPU_MEMORY_GB` | persisted preference | saved preference or platform default | Manual GPU memory ceiling in GB. |
| `CAVEVIEWER_IO_WORKERS` | persisted preference | saved preference or platform default | Max chunk-loading worker threads. |
| `CAVEVIEWER_IO_RESERVED_CPUS` | persisted preference | saved preference or platform default | Logical CPUs reserved from loading. |
| `CAVEVIEWER_UPLOAD_CHUNKS_PER_FRAME` | persisted preference | saved preference or platform default | Max ready chunks uploaded each frame. |
| `CAVEVIEWER_UPLOAD_GROUPS_PER_FRAME` | persisted preference | saved preference or platform default | Max render-thread upload slices from one ready chunk. |
| `CAVEVIEWER_UPLOAD_TIME_BUDGET_MS` | persisted preference | saved preference or platform default | Target milliseconds spent uploading chunks each frame. |
| `CAVEVIEWER_CHUNK_SIZE_METERS` | persisted preference | saved preference or platform default | Unitless chunk edge length for new caches. |
| `CAVEVIEWER_MAX_UPLOAD_GROUP_MB` | persisted preference | saved preference or platform default | Maximum VBO payload size for dense chunk groups, in MB. |
| `CAVEVIEWER_OBJ_SCAN_THROTTLE_MS` | persisted preference | saved preference or platform default | Milliseconds paused while scanning .obj files. |
| `CAVEVIEWER_OBJ_IMPORT_BATCH_FACES` | persisted preference | saved preference or platform default | Thousands of triangulated faces per batch. |
| `CAVEVIEWER_CHUNK_BUILD_WORKERS` | persisted preference | saved preference or platform default | Max cache-building worker threads. |
| `CAVEVIEWER_CHUNK_BUILD_RESERVED_CPUS` | persisted preference | saved preference or platform default | Logical CPUs reserved from cache build. |
| `CAVEVIEWER_RECORDING_DIR` | persisted preference | saved preference or platform default | Where saved recordings are stored. |
| `CAVEVIEWER_MAP_LIBRARY_DIR` | persisted preference | saved preference or platform default | Where CaveViewer stores downloaded Map Library maps. |
| `CAVEVIEWER_HOME` | environment | _(unset)_ | Optional absolute portable storage root. |
| `CAVEVIEWER_MAP_CACHE_DIR` | environment | _(unset)_ | Optional absolute root for generated map caches. |
| `CAVEVIEWER_APP_ICON` | environment | _(unset)_ | Optional custom application icon path. |
| `CAVEVIEWER_LOG_LEVEL` | environment | derived from runtime inputs | Application logging verbosity. |
| `CAVEVIEWER_FORCE_STARTUP_FOCUS` | environment | `False` | Whether a viewer may request foreground focus at startup. |
| `CAVEVIEWER_FORCE_UPDATE` | environment | `False` | Whether update presentation is forced for local testing. |
| `CAVEVIEWER_GITHUB_REPO` | environment | `CaveViewer/CaveViewer` | GitHub owner/repository used for update configuration. |
| `CAVEVIEWER_UPDATE_BRANCH` | environment | `main` | Git branch used to derive the default update manifest URL. |
| `CAVEVIEWER_UPDATE_CHANNEL` | environment | derived from runtime inputs | Update manifest channel; defaults to the channel embedded in the package. |
| `CAVEVIEWER_UPDATE_MANIFEST_URL` | environment | _(unset)_ | Optional full URL overriding the platform-derived update manifest. |
| `CAVEVIEWER_UPDATE_MANIFEST_SIGNATURE_URL` | environment | _(unset)_ | Optional full URL overriding the update-manifest signature location. |
| `CAVEVIEWER_IMPORT_NICE` | environment | `5` | Best-effort positive niceness increment for import worker processes. |
| `CAVEVIEWER_IO_NICE` | environment | `5` | Best-effort positive niceness increment for streaming workers. |
| `CAVEVIEWER_OBJ_BUCKET_WORKERS` | environment | `2` | Maximum worker count for incremental OBJ bucket preparation. |
| `CAVEVIEWER_MAX_TEXTURE_SIZE` | environment | _(unset)_ | Optional maximum texture dimension in pixels before decode. |
| `CAVEVIEWER_FFMPEG` | environment | _(unset)_ | Optional explicit ffmpeg executable used by recording. |
| `CAVEVIEWER_RECORDING_FPS` | environment | `30` | Target MP4 recording frame rate. |
| `CAVEVIEWER_RECORDING_MAX_HEIGHT` | environment | `720` | Maximum encoded recording height in pixels. |
| `CAVEVIEWER_RECORDING_CRF` | environment | `23` | H.264 recording quality value. |
| `CAVEVIEWER_TEXTURE_RESIDENT_CACHE_MB` | environment | _(unset)_ | Optional resident GPU texture-cache cap in MiB. |
| `CAVEVIEWER_TEXT_AA_MODE` | environment | derived from runtime inputs | FreeType text anti-aliasing mode. |
| `CAVEVIEWER_TK_SCALE` | environment | _(unset)_ | Optional Tk display scaling override. |
| `CAVEVIEWER_UI_FONT` | environment | _(unset)_ | Optional font path for the OpenGL text renderer. |
| `CAVEVIEWER_UI_TEXT_SCALE` | environment | `1.28` | Base scale for FreeType-rendered viewer overlay text. |
| `CAVEVIEWER_VIEWER_UI_SCALE` | environment | _(unset)_ | Optional viewer HUD scale; unset keeps automatic sizing. |
| `CAVEVIEWER_VSYNC` | environment | `True` | Whether the viewer waits for vertical sync. |
| `CAVEVIEWER_WINDOW_SYSTEM` | environment | `auto` | Requested Linux viewer window-system route. |
| `CAVEVIEWER_MAP_LIBRARY_REPO` / `CAVEVIEWER_SAMPLE_MAPS_REPO` | environment | `CaveViewer/CaveViewer` | GitHub owner/repository used by the Map Library source. |
| `CAVEVIEWER_MAP_LIBRARY_RELEASE_TAG` / `CAVEVIEWER_SAMPLE_DATA_TAG` | environment | `sample-data` | Release tag used by the Map Library source. |
| `CAVEVIEWER_MAP_LIBRARY_API_URL` / `CAVEVIEWER_SAMPLE_MAPS_API_URL` | environment | derived from runtime inputs | GitHub release API URL used by the Map Library source. |
| `CAVEVIEWER_MAP_LIBRARY_CATALOG_ASSET_NAME` | environment | `caveviewer-map-library.v1.json` | Catalog asset name used by the Map Library source. |
<!-- END RUNTIME_SETTINGS_TABLE -->

### Development & Launch

| Variable | Default | Description |
|---|---|---|
| `CAVEVIEWER_DEV_VENV` | `.venv-dev` | Path to the development virtual environment used by `run_caveviewer.sh` and `scripts/dev/install.sh`. |
| `CAVEVIEWER_MACOS_BUILD_VENV` | _(none)_ | Path to the venv used by the macOS build scripts. |
| `CAVEVIEWER_LINUX_BUILD_VENV` | _(none)_ | Path to the venv used by the Linux build scripts. |
| `CAVEVIEWER_PROJECT_ROOT` | _(set by `scripts/dev/env_setup.sh`)_ | Source checkout root used only by development shell helpers; it is not a user-storage location. |
| `CAVEVIEWER_HOME` | _(none)_ | Absolute portable-storage root. CaveViewer derives `config`, `data`, `cache`, `state`, and `runtime` children beneath it. |
| `CAVEVIEWER_MAP_CACHE_DIR` | _(none)_ | Advanced override for generated map caches. When unset, CaveViewer writes `_cache` inside the source map folder. When set, it must be an absolute root for hashed generated caches. |
| `CAVEVIEWER_APP_ICON` | _(bundled icon)_ | Path to a custom application icon file. |
| `CAVEVIEWER_FORCE_STARTUP_FOCUS` | `0` | Set to `1` to force the main window to the front on startup. Disabled by default on frozen macOS builds to avoid window-placement jumps. |
| `CAVEVIEWER_LOG_LEVEL` | `INFO` | Logging verbosity. Accepted values: `DEBUG`, `INFO`, `WARNING`, `ERROR`. |

### Update Checking

| Variable | Default | Description |
|---|---|---|
| `CAVEVIEWER_GITHUB_REPO` | `CaveViewer/CaveViewer` | The GitHub `owner/repo` used to build the default update manifest URL and map-library API URL. Override when running a fork or testing a package from Terminal. |
| `CAVEVIEWER_UPDATE_BRANCH` | `main` | Git branch used when deriving the default `raw.githubusercontent.com` update manifest URL. Also available as `--update-branch <branch>` for update testing from a non-`main` branch. Ignored when `CAVEVIEWER_UPDATE_MANIFEST_URL` is set. |
| `CAVEVIEWER_UPDATE_CHANNEL` | embedded package channel | Deliberate developer/testing override for the update manifest channel used when deriving the default manifest URL. Accepted values: `stable`, `preview`. A source checkout and a historical package without metadata safely default to `stable`. Ignored when `CAVEVIEWER_UPDATE_MANIFEST_URL` is set. |
| `CAVEVIEWER_UPDATE_MANIFEST_URL` | _(derived from repo)_ | Full URL to the JSON update manifest. Overrides the default `raw.githubusercontent.com` path. Useful for pointing at a staging manifest or a custom server. |
| `CAVEVIEWER_UPDATE_MANIFEST_SIGNATURE_URL` | `<manifest-url>.sig` | Full URL to the base64 Ed25519 signature for the update manifest. |
| `CAVEVIEWER_FORCE_UPDATE` | `0` | Set to `1` (or `true`/`yes`) to treat a valid, signed, available manifest artifact as newer regardless of its version. Also available as `--force-update`. It cannot fabricate a missing channel or make an unavailable package eligible. |
| `CAVEVIEWER_MACOS_ARCH` | _(auto)_ | Low-level macOS packaging override. The top-level release dispatcher uses `--target=macos-arm64` or `--target=macos-x86_64`; normal app update checks detect the running process architecture automatically. |
| `CAVEVIEWER_LINUX_UPDATE_ARCH` | `x86_64` | Linux publish helper only. Linux distribution is x86_64-only; set to `x86_64` when invoking lower-level publish helpers directly. |

The update checker requires manifests to be signed by a trusted release
Ed25519 identity. The bundled primary, offline-recovery, and retained legacy
public keys live under `src/caveviewer/resources/`. Startup update
checks read the branch/channel manifest first; if it advertises a newer version,
the app checks those keys in primary, recovery, legacy order and confirms that the package URL
resolves before offering the download. Missing or invalid signatures and
unavailable packages are logged and do not expose an update action. An absent
preview manifest is the normal empty-channel state and likewise leaves the
splash unchanged. The release finalizer creates every requested companion
`.sig` only after GitHub confirms the uploaded asset's URL, size, and SHA-256,
then commits the manifest pairs together. See
[`releases.md`](releases.md) for the full
release contract.

`caveviewer.app` owns one `UpdateManager` for the full GUI process. Its explicit
state machine is:

```text
IDLE -> CHECKING -> {UP_TO_DATE, AVAILABLE, IDLE on check error}
AVAILABLE -> DOWNLOADING -> VERIFYING -> READY -> HANDOFF_VERIFYING -> INSTALLING -> SHUTDOWN
                |              |          |                  |
                +--------------+-> FAILED -> DOWNLOADING     +-> READY (handoff failure/cancel)
(DOWNLOADING or VERIFYING) -- cancel request --> worker cleanup --> AVAILABLE
any non-SHUTDOWN state -> SHUTDOWN
```

The splash polls immutable manager snapshots and maps the visible states to
`Update to <version>`, `Downloading… <percentage>%` with `Cancel`,
`Verifying…` with `Cancel`, `Update ready`, and `Download failed` with a
separate `Retry` action. A normal ready update remains a single footer label:
after three seconds, `Update ready` becomes the focused platform action that
reveals the already verified package: `Show in Finder` on macOS, `Show in
Explorer` for a Windows ZIP migration package, or `Open Download Folder` on
Linux. A registered Windows EXE whose signed update manifest declares either
the default Authenticode policy or the explicit unsigned-community policy
instead presents `Install and restart <version>` immediately. Its explicit
click downloads the EXE, starts the
handoff after promotion, and the splash exits only after the installer process
starts. That click is the consent boundary: the Inno Setup progress window
remains visible, but its suppressible messages use their declared defaults so a
normal update needs no second confirmation. Windows-owned trust warnings remain
user-controlled. A cancellation request only signals the manager worker; it
cleans staging output and returns to the available update without affecting an
already verified package.
While a splash window is visible, it is the foreground update surface and
suppresses duplicate desktop notifications for update progress or completion.
If a download finishes after that surface closes, desktop notifications remain
available for background completion or failure.
Neither the viewer nor streaming code depends on the update manager. Opening a
map closes only that splash instance, so an active download continues and a
later splash presents its current state. Closing the whole app moves the
manager to `SHUTDOWN`, cancels any active transfer, waits for its worker, and
removes the temporary staging directory.

A verified package is atomically promoted through a hidden temporary sibling.
macOS, Linux, and Windows ZIP migration packages go to `~/Downloads` and stay
manual: a visible splash makes one automatic reveal attempt and retains a
manual platform action. A Windows EXE from a registered frozen Inno Setup
installation goes to `%LOCALAPPDATA%\CaveViewer\updates` instead. The handoff
always rechecks size/SHA-256. The default verified policy also rechecks
Authenticode status, exact publisher, and timestamp; the explicit
unsigned-community policy instead relies on the already verified signed
manifest and requires no publisher. It then starts the installer with distinct
arguments.
The installer validates `--expected-version`, waits at most five minutes for
`--wait-pid`, verifies the new payload, records its per-user provenance marker,
and relaunches it. A source/ZIP launch has no marker and must use the manual
migration action; it never executes an EXE update.

On Linux, every directory chooser also uses XDG Desktop Portal when available.
Cancellation is distinct from portal failure; unavailable or old portals fall
back to the owned Tk chooser. Portal-selected source folders need only be
readable to open an existing cache. Building a new cache or rebuilding one
also requires a safe writable cache destination: the source map's `_cache`
directory by default, or an absolute `CAVEVIEWER_MAP_CACHE_DIR` override.

Default update checks read committed main-branch manifests, not GitHub's
latest-release metadata. A frozen package selects its own `stable` or
`preview` channel through embedded release metadata: macOS uses
`updates/macos/<arm64|x86_64>/<channel>.json` and selects the running process
architecture, so a Rosetta-launched x86_64 build stays on the Intel channel.
Linux distribution is x86_64-only and uses
`updates/linux/x86_64/<channel>.json`; Linux ARM64 builds do not receive
automatic updates. A source checkout and historical package without embedded
metadata use `stable`. Stable publish updates and signs `stable.json`;
Preview publishing updates the separate `preview.json`, leaving Stable
unchanged and marking a newly created GitHub release as a GitHub prerelease. For
debugging, explicit environment variables can point a source run or packaged
app launched from Terminal at another branch, channel, or manifest URL.
Until that target has a verified published preview, its preview manifest
and signature are intentionally absent.

Preview branch testing can use the derived preview manifest URL after the
selected branch contains the matching platform manifest and signature. For
macOS, confirm the branch contains both files for the process architecture:

```bash
git ls-tree -r release/<version> updates/macos
```

The output must include:

```text
updates/macos/<arm64|x86_64>/preview.json
updates/macos/<arm64|x86_64>/preview.json.sig
```

Then test from a source checkout with `--update-branch` and `--force-update`:

```bash
CAVEVIEWER_UPDATE_CHANNEL=preview \
./run_caveviewer.sh --update-branch release/<version> --force-update
```

For a packaged app launched from Terminal, use environment variables instead:

```bash
CAVEVIEWER_FORCE_UPDATE=1 \
CAVEVIEWER_UPDATE_BRANCH=release/<version> \
CAVEVIEWER_UPDATE_CHANNEL=preview \
./CaveViewer-<version>-x86_64.AppImage
```

For a preview channel, a missing derived manifest logs `No preview update
manifest is published` at info level and means no preview is currently
available for that platform. Publish a real package through the finalizer,
switch to a branch that already advertises one, or use
`CAVEVIEWER_UPDATE_CHANNEL=stable` if you meant to test stable updates. HTTP 404
for a stable or explicitly configured non-preview manifest remains a
configuration error.

If the update checker logs `Update manifest fetch failed with HTTP 429`, GitHub
has rate-limited the unauthenticated `raw.githubusercontent.com` request. The
splash interface stays unchanged. Wait for the limit to clear, switch networks,
or set `CAVEVIEWER_UPDATE_MANIFEST_URL` to a staging/custom-hosted copy of the
manifest; the signature URL defaults to `<manifest-url>.sig` unless
`CAVEVIEWER_UPDATE_MANIFEST_SIGNATURE_URL` is set explicitly.

Linux manifests are x86_64-only:

```text
updates/linux/x86_64/stable.json
updates/linux/x86_64/preview.json
```

macOS manifests are also architecture-specific:

```text
updates/macos/arm64/stable.json
updates/macos/arm64/preview.json
updates/macos/x86_64/stable.json
updates/macos/x86_64/preview.json
```

Each preview pair appears only after that platform/channel is successfully
published; an absent pair is not an error. Top-level
`updates/macos/<channel>.json[.sig]` files are legacy ARM64 aliases when that
ARM64 channel exists. Keep each alias and signature byte-for-byte identical to
its `arm64/` counterpart so older installations continue receiving updates.

macOS DMG assets include their architecture to prevent uploads from replacing
one another on a shared GitHub release:

```text
CaveViewer-<version>-macos-arm64.dmg
CaveViewer-<version>-macos-x86_64.dmg
```

Sign a manifest:

```bash
python3 scripts/sign_update_manifest.py \
  updates/macos/arm64/stable.json \
  --private-key /path/to/release_signing_private_key.pem
```

This writes `updates/macos/arm64/stable.json.sig`. An ARM64 publish copies the
signed manifest and signature to the top-level legacy aliases. Release publish
scripts do not use a default private-key path; set
`CAVEVIEWER_RELEASE_SIGNING_PRIVATE_KEY` to the deliberately selected private
key before publishing either channel. When signing manually, either set that
variable or pass `--private-key`; verify it matches the intended bundled public
identity first.

### UI & Rendering

#### OpenGL UI scaling

The OpenGL viewer renders its overlay text directly with FreeType in screen
pixels. It automatically rasterizes glyphs at the active framebuffer pixel
ratio so high-DPI and fractional-scale desktops do not stretch low-resolution
font bitmaps. The always-visible viewer HUD also grows automatically on larger
viewer surfaces so AppImage/XWayland and maximized GNOME windows keep legible
controls without asking users to set environment variables.

Viewer window geometry is separate from overlay text scaling. On Linux it is
derived from 80% of GLFW's primary-monitor work area in backend-native screen
coordinates. GLFW continues to scale the framebuffer for high-DPI rendering,
but its X11 `SCALE_TO_MONITOR` size expansion is suppressed because applying it
to an already monitor-relative window would scale the geometry twice.

The accepted range is `0.5` through `3.0`. The controls/help overlay derives
its row height from the resulting FreeType line metrics, so increasing the text
scale also reserves enough vertical space for each line and its keycap. Text
inside the right-side control panel uses its own responsive HUD scale so button
geometry and labels grow together.

For development-only visual tuning, `CAVEVIEWER_UI_TEXT_SCALE` adjusts
adaptable full-screen overlay text and `CAVEVIEWER_VIEWER_UI_SCALE` overrides
the automatic right-side HUD scale. These are not required for normal users.
macOS uses a slightly larger default overlay text scale because the viewer HUD
is rendered through FreeType/OpenGL and does not inherit macOS Accessibility
Text Size preferences the way compatible native controls can.
Tk startup fonts are also scaled from Tk's runtime `TkDefaultFont` point size,
with a 1.4x macOS readability floor, before the splash screen and map library
create their labels and buttons. The macOS splash uses a larger baseline window
than Windows/Linux so the larger startup fonts are not forced back into the old
compact layout.

| Variable | Default | Description |
|---|---|---|
| `CAVEVIEWER_UI_TEXT_SCALE` | `1.28` (`1.472` on macOS) | Development override for adaptable in-app overlay text such as loading screens, controls/help overlay, and status readouts. Right-side viewer controls use `CAVEVIEWER_VIEWER_UI_SCALE` instead so labels and button geometry stay matched. |
| `CAVEVIEWER_VIEWER_UI_SCALE` | `auto` | Development override for the always-visible viewer HUD scale. By default CaveViewer keeps the compact baseline at 1536x864 and grows the right-side controls on larger viewer surfaces. |
| `CAVEVIEWER_TK_SCALE` | _(display DPI)_ | Windows/Linux override for Tk interface scaling, clamped to `0.75` through `4.0`. The Linux AppImage launcher normally derives this value from the desktop Xft DPI setting. |
| `CAVEVIEWER_APPRUN_INSTALL_ONLY` | `0` | Linux AppImage launcher smoke mode. Set to `1` to install/update the desktop file, AppStream metadata, and hicolor icons in the user's XDG data home, print the installed paths, and exit without launching the GUI. |
| `CAVEVIEWER_APPRUN_UNINSTALL` | `0` | Linux AppImage launcher uninstall mode. Set to `1` to remove CaveViewer's per-user desktop file, AppStream metadata, and hicolor icons, then exit without launching the GUI. It does not remove maps, settings, caches, or downloaded update packages. |
| `CAVEVIEWER_NO_DESKTOP_INTEGRATION` | `0` | Linux AppImage launcher opt-out. Set to `1` to skip the best-effort per-user desktop integration step. |
| `CAVEVIEWER_UI_FONT` | _(platform default)_ | Absolute path to a `.ttf`/`.otf`/`.ttc` font file for the in-app FreeType renderer. Overrides the platform font search order. |
| `CAVEVIEWER_TEXT_AA_MODE` | `light` (macOS/Linux), `normal` (others) | FreeType anti-aliasing mode for in-app text. `normal` = standard hinting; `light` = smooth light anti-aliasing; `lcd` = LCD sub-pixel rendering. |
| `CAVEVIEWER_VSYNC` | `1` | Set to `0` to disable vertical sync when diagnosing display-driver stalls. |
| `CAVEVIEWER_WINDOW_SYSTEM` | `auto` | Linux viewer backend: `auto` prefers X11/XWayland when `DISPLAY` is available so source and AppImage launches get the same GNOME titlebar and resize behavior, then retries Wayland on recognized initialization failures. `wayland` and `x11` require that protocol without fallback. |
| `LIBGL_ALWAYS_SOFTWARE` | _(unset)_ | Linux OpenGL/Mesa setting. Set to `1` to force software rendering when a GPU driver crashes, freezes, or leaves the app stuck in the graphics driver. |
| `CAVEVIEWER_FFMPEG` | _(auto)_ | Path to an `ffmpeg` executable for MP4 recording. If unset, CaveViewer tries system `ffmpeg`, then the bundled `imageio-ffmpeg` executable. |
| `CAVEVIEWER_RECORDING_DIR` | `~/Movies/CaveViewer` | Folder where saved recordings are stored. The Preferences panel saves this value. |
| `CAVEVIEWER_MAP_LIBRARY_DIR` | User Downloads folder | Folder where CaveViewer stores downloaded Map Library maps. The Preferences panel saves this value. |
| `CAVEVIEWER_RECORDING_FPS` | `30` | Target MP4 recording frame rate. Range: 1–60. Frames are streamed to `ffmpeg`; they are not buffered in memory. |
| `CAVEVIEWER_RECORDING_MAX_HEIGHT` | `720` | Maximum output video height. The framebuffer is downscaled before encoding to reduce render readback and encoding cost. Set to `1080` to opt back into 1080p recording. |
| `CAVEVIEWER_RECORDING_CRF` | `23` | H.264 quality value passed to `ffmpeg`. Lower is larger/higher quality; higher is smaller/lower quality. Range: 0–51. |

### Preferences and Rendering Architecture

Preferences opens numeric fields with their effective defaults. Numeric
inputs use a compact, consistent width. If a numeric value is
cleared, only its accepted range immediately appears inside the input as
muted, unit-free `minimum-maximum` placeholder text without comparison
operators; the placeholder itself is never applied or saved as a value.
Every field is validated as it changes. An invalid value is highlighted and
keeps the shared validation message visible while the other inputs become
temporarily read-only and the Apply button is disabled. Read-only inputs retain
their normal dark appearance. Correcting the value immediately unlocks the
form; valid values are normalized when focus leaves the field. A focused
required field may remain temporarily empty while the user replaces its value;
Apply is disabled immediately while any required value is blank, while the
required message and read-only form lock appear only after focus leaves that
empty field. Cancel discards unapplied edits; Escape navigation and closing the
startup window ask before discarding them. Valid worker counts are treated as
advisory caps and do not show a bottom warning.

The Preferences implementation is split by responsibility:
`src/caveviewer/core/preferences/schema.py` owns the typed `PreferenceSpec` schema,
validation, defaults, and environment mapping;
`src/caveviewer/core/preferences/runtime_settings.py` owns the declarative
runtime-setting inventory and immutable, provenance-bearing composition snapshot
without importing GUI code;
`src/caveviewer/gui/preferences.py` owns preferences persistence and legacy
migration while re-exporting the core preferences API for GUI callers;
`src/caveviewer/gui/preference_paths.py` owns preference/state file locations,
legacy migration, and atomic text writes;
`src/caveviewer/gui/preferences_form.py` owns focus/change/blur/apply
state transitions; `src/caveviewer/gui/preferences_dialog.py` only
renders that state into Tk widgets; and
`src/caveviewer/core/workers/allocation.py` resolves the effective streaming/import
worker counts while honoring reserved logical CPUs and owns the shared RAM
admission cutoff. Both pools start with one worker and admit at most one more
after completed work has been measured; they stay at their current concurrency
when system RAM utilization reaches 80% or current availability cannot be
measured. Only immutable, validated `Preferences` snapshots may cross into
persistence. The application-owned `RuntimeSettingsSession` resolves saved
values, environment values, and command-line overrides into one immutable
runtime snapshot; a successful Preferences save replaces that snapshot for
later viewer or Map Library actions without mutating the process environment.
Invalid saved or environment values fall back independently to that field's
valid default, so one stale value does not discard the rest of the
configuration. Preferences are saved through an atomic temporary-file
replacement; a write failure remains visible in the embedded Preferences panel
and does not alter the previous preferences file.

First-time map imports are isolated from the viewer event loop by
`src/caveviewer/gui/import_process.py`. The viewer process keeps OpenGL/window
events and progress rendering responsive while a spawned child process runs the
core `map.importer.import_and_cache_any()` path through app-level compatibility
wrappers. The child sends progress, completion, heartbeat, and traceback-bearing
failure events back to the viewer. Closing the viewer requests import
shutdown, briefly joins the parent-side relay worker, terminates any reachable
active child import process, and removes abandoned private staging directories
for that map cache when termination completes. The child also lowers its
desktop scheduling priority and caps common native compute libraries to one
thread unless the user has already set those library-specific variables.

The splash screen and its embedded Preferences and Map Library panels share Tk
color and control tokens through `src/caveviewer/gui/tk_theme.py`. Map-folder
validation lives in `src/caveviewer/gui/map_selection.py`, allowing map-opening
entry points to reuse it without importing private splash-screen implementation
details.

CaveViewer keeps exactly one Tk root per process. On macOS the splash root is
withdrawn, kept alive for the global app-menu callbacks, and reused on later
splash cycles. Viewer sizing also reuses an existing Tk root for screen
dimensions before falling back to a temporary root in non-splash launch paths.

The OpenGL viewer render callback must return promptly. Low-activity states
such as minimized windows, startup import progress, and startup import pause
notices use timestamp-based redraw gates instead of `time.sleep()` so queued
window, input, and background-task events are not blocked on the render thread.

Runtime chunk streaming is also split by policy boundary:
`src/caveviewer/core/hardware/system_memory.py` detects total and currently
available system RAM on Windows, macOS, and Linux;
`src/caveviewer/core/hardware/gpu_memory.py` detects or estimates the active GPU
memory budget;
`src/caveviewer/core/hardware/memory_targets.py` parses RAM and GPU utilization
targets;
`src/caveviewer/core/streaming/budget.py` contains pure
chunk-size estimation and residency-cap calculation;
`src/caveviewer/core/streaming/scheduler.py` owns the bounded ready backlog,
spatial selection, and eviction policy; and
`src/caveviewer/core/streaming/world.py` coordinates worker lifecycle and
render-thread callbacks. Map imports now write only the cache artifacts used by
runtime streaming and the minimap. Caches and their texture assets are
atomically published to `_cache` inside the source map folder by default.
`CAVEVIEWER_MAP_CACHE_DIR` can place hashed generated caches under a separate
absolute root; the older `.caveviewer_cache` directory is not auto-discovered.

Runtime rendering variables, import tuning variables, low-memory launch
examples, and `caveviewer-chunker` CLI options are listed in
[`rendering.md`](rendering.md).

### Application storage locations

Unless overridden, CaveViewer stores files in these locations:

| Kind | Linux default | macOS/Windows default |
|---|---|---|
| Preferences | `$XDG_CONFIG_HOME/caveviewer/advanced_settings.json` (`~/.config/...` fallback; legacy-compatible filename) | `~/.caveviewer/advanced_settings.json` |
| Remembered chooser locations | `$XDG_STATE_HOME/caveviewer/` (`~/.local/state/...` fallback) | `~/.caveviewer/` |
| Windows pre-splash diagnostics | — | `~/.caveviewer/diagnostics/startup.log` |
| Windows viewer-session diagnostics | — | `~/.caveviewer/diagnostics/viewer-session-<id>.log` and `.jsonl` |
| Map caches | Source map folder `/_cache` | Source map folder `/_cache` |
| Downloaded map library | `$XDG_DOWNLOAD_DIR/` (`~/Downloads/` fallback) | `~/Downloads/` |

`CAVEVIEWER_HOME` creates isolated `config`, `data`, `cache`, `state`, and
`runtime` children; map caches still default to each source map folder's
`_cache` subdirectory. `CAVEVIEWER_MAP_CACHE_DIR` overrides only the generated
map-cache root for advanced runs that need caches on a separate filesystem.
`CAVEVIEWER_MAP_LIBRARY_DIR` overrides the folder used for downloaded
map-library entries. Older child `map_library` and `sample_maps` layouts remain
readable as legacy locations. On Linux, old `~/.caveviewer/` and
`~/.caveviewer_*` files are copied once and left in place, and older app-data
`map_library` or `sample_maps` directories are moved into the configured
map-library location when possible. A generated cache is self-contained:
texture files are staged beside its chunks before the manifest becomes visible.

When a Windows build consumes CPU without showing its Tk splash, read
`%USERPROFILE%\.caveviewer\diagnostics\startup.log`. The file contains the last
flushed startup checkpoint and, after 20 seconds without a visible splash, one
all-thread Python traceback. A normal visible splash closes the diagnostic file
immediately.

If selecting a map closes the Windows app before a viewer appears, collect the
newest `%USERPROFILE%\.caveviewer\diagnostics\viewer-session-*.log` and its
matching `.jsonl` file. The text log records map selection, viewer launch,
native-window/context construction, and the first render callback. It also
receives Python fatal-fault tracebacks when available; the JSONL file contains
the same session's structured application events and caught exception
tracebacks. Each app run uses a new id, so a later retry does not overwrite the
previous failure's logs.

Disk-space checks therefore target the filesystem that will hold the cache,
which is normally the map's filesystem unless an explicit cache root is set.

Successful Map Library downloads are also recorded in a private versioned
managed-install registry in application state. The registry contains only
source-qualified map identity, app-owned install paths, and the last confirmed
former/current state; it is used to show a locally downloaded map that has
subsequently disappeared from an authoritative source catalog, including on an
offline restart. It does not scan arbitrary personal map folders. GitHub maps
retain the established direct Downloads layout; future non-GitHub sources use
an app-managed source subdirectory to avoid source-name collisions.

### Map Library

| Variable | Default | Description |
|---|---|---|
| `CAVEVIEWER_MAP_LIBRARY_DIR` | User Downloads folder | Folder where CaveViewer stores downloaded Map Library maps. |
| `CAVEVIEWER_MAP_LIBRARY_REPO` | `CaveViewer/CaveViewer` | GitHub `owner/repo` for the map library release. |
| `CAVEVIEWER_MAP_LIBRARY_RELEASE_TAG` | `sample-data` | Release tag to fetch CaveViewer map assets from. |
| `CAVEVIEWER_MAP_LIBRARY_API_URL` | _(derived from repo + tag)_ | Full GitHub release API URL. Overrides the repo/tag variables when set. |
| `CAVEVIEWER_MAP_LIBRARY_CATALOG_ASSET_NAME` | `caveviewer-map-library.v1.json` | Release asset name for the map-library catalog manifest. |

Legacy aliases remain supported at lower precedence:
`CAVEVIEWER_SAMPLE_MAPS_REPO`, `CAVEVIEWER_SAMPLE_DATA_TAG`, and
`CAVEVIEWER_SAMPLE_MAPS_API_URL`.

The embedded Map Library panel keeps Tk work on the Tk thread. Catalog fetches
and map-library download/extract work run in background workers; workers publish
progress and terminal status through queues, and the panel applies those
messages from `after()` callbacks. Worker callbacks must not read or mutate Tk
widgets directly. Closing the startup window cancels the active download and
its pending queue poll callback.

After a successful authoritative refresh, a downloaded map no longer offered
by its source stays in its prior position in **CaveViewer Maps** with a muted
title and the message **No longer a part of the standard library**. It is
still a normal local map: it can open, use Guided Dive, rebuild or remove cache
data, and remove its files. It cannot download or update because its source no
longer offers it. Its three-dot menu exposes the same local management actions
as other downloaded maps.

Eligible recent and downloaded rows also offer `Rebuild cache`. It starts a
forced child import using the current Import preferences without opening a
viewer, revalidating the cache target and source at action time. The prior
cache remains usable until the staged replacement publishes; OBJ rebuilds can
be paused. If the splash no longer has input focus, a completion or failure may
also use the optional desktop-notification route.
