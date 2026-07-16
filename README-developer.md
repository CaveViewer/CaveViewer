# CaveViewer Development From Source

This guide is for users who want to run CaveViewer from source.

Scope:

- This document is intentionally focused on source-based development and local runs.

Contributor workflow, architecture, repository layout, coding, testing, and
AI-assistant guidance are indexed in
[`docs/development/`](docs/development/README.md). See
[`CONTRIBUTING.md`](CONTRIBUTING.md) before preparing a change.
The canonical platform release sequence and verification checklist are in
[`docs/development/releases.md`](docs/development/releases.md).

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

The application About text should identify CaveViewer as licensed under the GNU General Public License version 3.0.

## Requirements

- Git
- Python 3.10+

You also need to run a typical workstation setup with C++ and other compilers if you desired to compile from source.

Ubuntu should work out of the box. Fedora 44 is special, so you have to install additional packages
```bash
sudo dnf install gcc gcc-c++ make python3-devel \
    mesa-libGL-devel mesa-libEGL-devel libX11-devel sudo python3.14-tkinter
```

## Clone the Repository

```bash
git clone https://github.com/KernalPanic/CaveViewer.git
cd CaveViewer
```

Optional: check out the latest version tag.

```bash
git fetch --tags
latest=$(git tag -l "v*" --sort=-version:refname | head -n 1)
git checkout "$latest"
```

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
py -3 -m venv .venv-dev
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

- `scripts/windows/setup.ps1` is designed to install prerequisites and set up a runnable local source environment.
- `scripts/windows/launch.bat` is a launcher for the setup script.

## Run Automated Tests

Install the development-only test tools after the runtime dependencies:

```bash
.venv-dev/bin/python -m pip install -r requirements-dev.txt
.venv-dev/bin/python -m pytest
```

On Windows, use `.venv-dev\Scripts\python` in place of `.venv-dev/bin/python`.

The suite isolates the home/preferences directory, blocks uncontrolled network
connections, and uses temporary directories for all generated files. The same
essential suite and branch-coverage gate run automatically for pull requests
and before GitHub release builds. `All Platform Release` runs it once for the
whole parallel package fan-out; a directly dispatched platform workflow runs its own
gate. Direct `scripts/release.sh` runs also execute the complete pytest suite
before changing the application version or creating artifacts. It uses
`.venv-dev` when available, then falls back to
`python3`/`python`; set `CAVEVIEWER_TEST_PYTHON=/path/to/python` to select
another prepared interpreter. The interpreter must have `requirements.txt` and
`requirements-dev.txt` installed.

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

## Sample Map Source Overrides

By default, the built-in sample maps dialog reads release assets from:

- Repository: `KernalPanic/CaveViewer`
- Release tag: `sample-data`

For local development, you can point the sample maps dialog at a different source before launching the program. These settings are environment variables only; they are not exposed in the app UI.

Precedence:

1. `CAVEVIEWER_SAMPLE_MAPS_API_URL` uses a full release API URL directly.
2. Otherwise, CaveViewer builds the GitHub release API URL from `CAVEVIEWER_SAMPLE_MAPS_REPO` and `CAVEVIEWER_SAMPLE_DATA_TAG`.
3. If none are set, the defaults above are used.

macOS/Linux example:

```bash
CAVEVIEWER_SAMPLE_MAPS_REPO="MyOrg/MyMaps" \
CAVEVIEWER_SAMPLE_DATA_TAG="public-samples" \
./run_caveviewer.sh
```

Windows PowerShell example:

```powershell
$env:CAVEVIEWER_SAMPLE_MAPS_REPO = "MyOrg/MyMaps"
$env:CAVEVIEWER_SAMPLE_DATA_TAG = "public-samples"
.\.venv-dev\Scripts\python -m caveviewer
```

Advanced direct API override:

```bash
CAVEVIEWER_SAMPLE_MAPS_API_URL="https://api.github.com/repos/MyOrg/MyMaps/releases/tags/public-samples" \
./run_caveviewer.sh
```

The API response must be compatible with GitHub's release API shape, including an `assets` list with asset `name`, `browser_download_url`, and `size` fields.

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

- `python3 not found` (macOS/Linux): install Python 3.10+ and rerun setup.
- Broken `.venv-dev`: remove it and rerun `./scripts/dev/install.sh`.
- Windows PowerShell policy blocks setup script: run with `-ExecutionPolicy Bypass` as shown above.

### Virtual Machine Runs

When running CaveViewer inside Parallels or another VM, launch with vsync
disabled. Some virtual GPU drivers can hang or crash when vsync is enabled.

```bash
CAVEVIEWER_VSYNC=0 ./run_caveviewer.sh
```

If the VM or GPU driver still hangs or crashes, force software OpenGL rendering:

```bash
LIBGL_ALWAYS_SOFTWARE=1 CAVEVIEWER_VSYNC=0 ./run_caveviewer.sh
```

`LIBGL_ALWAYS_SOFTWARE=1` bypasses the GPU driver and asks Mesa to render in
software. It may be slower, but it is useful on VMs or machines with unreliable
OpenGL drivers.

For large maps in a VM, also reduce per-frame GPU upload pressure:

```bash
LIBGL_ALWAYS_SOFTWARE=1 \
CAVEVIEWER_VSYNC=0 \
CAVEVIEWER_UPLOAD_CHUNKS_PER_FRAME=1 \
CAVEVIEWER_UPLOAD_TIME_BUDGET_MS=1 \
./run_caveviewer.sh
```

Do not rely on VM auto-detection; set these variables explicitly.

---

## Environment Variables

All variables are optional. Set them in your shell before launching or prefix them inline:

```bash
CAVEVIEWER_LOG_LEVEL=DEBUG ./run_caveviewer.sh
```

### Development & Launch

| Variable | Default | Description |
|---|---|---|
| `CAVEVIEWER_DEV_VENV` | `.venv-dev` | Path to the development virtual environment used by `run_caveviewer.sh` and `scripts/dev/install.sh`. |
| `CAVEVIEWER_MACOS_BUILD_VENV` | _(none)_ | Path to the venv used by the macOS build scripts. |
| `CAVEVIEWER_LINUX_BUILD_VENV` | _(none)_ | Path to the venv used by the Linux build scripts. |
| `CAVEVIEWER_PROJECT_ROOT` | _(set by `scripts/dev/env_setup.sh`)_ | Source checkout root used only by development shell helpers; it is not a user-storage location. |
| `CAVEVIEWER_HOME` | _(none)_ | Absolute portable-storage root. CaveViewer derives `config`, `data`, `cache`, `state`, and `runtime` children beneath it. |
| `CAVEVIEWER_MAP_CACHE_DIR` | Platform cache root + `/maps` | Absolute root for generated map caches. Defaults to `$XDG_CACHE_HOME/caveviewer/maps` on Linux (`~/.cache/...` fallback) and `~/.caveviewer/maps` on macOS/Windows; `CAVEVIEWER_HOME` uses `<root>/cache/maps`. CaveViewer no longer auto-discovers adjacent `_cache` or `.caveviewer_cache` directories. |
| `CAVEVIEWER_APP_ICON` | _(bundled icon)_ | Path to a custom application icon file. |
| `CAVEVIEWER_FORCE_STARTUP_FOCUS` | `0` | Set to `1` to force the main window to the front on startup. Disabled by default on frozen macOS builds to avoid window-placement jumps. |
| `CAVEVIEWER_LOG_LEVEL` | `INFO` | Logging verbosity. Accepted values: `DEBUG`, `INFO`, `WARNING`, `ERROR`. |

### Update Checking

| Variable | Default | Description |
|---|---|---|
| `CAVEVIEWER_GITHUB_REPO` | `KernalPanic/CaveViewer` | The GitHub `owner/repo` used to build the default update manifest URL and sample-maps API URL. Override when running a fork or testing a package from Terminal. |
| `CAVEVIEWER_UPDATE_BRANCH` | `main` | Git branch used when deriving the default `raw.githubusercontent.com` update manifest URL. Also available as `--update-branch <branch>` for update testing from a non-`main` branch. Ignored when `CAVEVIEWER_UPDATE_MANIFEST_URL` is set. |
| `CAVEVIEWER_UPDATE_CHANNEL` | `stable` | Update manifest channel used when deriving the default manifest URL. Accepted values: `stable`, `prerelease`. Ignored when `CAVEVIEWER_UPDATE_MANIFEST_URL` is set. |
| `CAVEVIEWER_UPDATE_MANIFEST_URL` | _(derived from repo)_ | Full URL to the JSON update manifest. Overrides the default `raw.githubusercontent.com` path. Useful for pointing at a staging manifest or a custom server. |
| `CAVEVIEWER_UPDATE_MANIFEST_SIGNATURE_URL` | `<manifest-url>.sig` | Full URL to the base64 Ed25519 signature for the update manifest. |
| `CAVEVIEWER_FORCE_UPDATE` | `0` | Set to `1` (or `true`/`yes`) to enter the update-available state regardless of the manifest version. Also available as `--force-update`. For testing the update UI without waiting for the CDN cache or changing version numbers. |
| `CAVEVIEWER_MACOS_ARCH` | _(auto)_ | Low-level macOS packaging override. The top-level release dispatcher uses `--target=macos-arm64` or `--target=macos-x86_64`; normal app update checks detect the running process architecture automatically. |
| `CAVEVIEWER_LINUX_UPDATE_ARCH` | `x86_64` | Linux publish helper only. Linux distribution is x86_64-only; set to `x86_64` when invoking lower-level publish helpers directly. |

The update checker requires manifests to be signed with the release Ed25519
private key. The bundled public key lives at
`src/caveviewer/resources/release_signing_public_key.pem`. Startup update
checks read the branch/channel manifest first; if it advertises a newer version,
the app verifies the manifest signature before offering the download. Missing or
invalid signatures are logged as errors and do not change the splash interface.
The release finalizer creates every requested companion `.sig` file before
committing the manifests together. See
[`docs/development/releases.md`](docs/development/releases.md) for the full
release contract.

`caveviewer.app` owns one `UpdateManager` for the full GUI process. Its explicit
state machine is:

```text
IDLE -> CHECKING -> {UP_TO_DATE, AVAILABLE, IDLE on check error}
AVAILABLE -> DOWNLOADING -> VERIFYING -> READY
                |              |
                +--------------+-> FAILED -> DOWNLOADING (retry)
any non-SHUTDOWN state -> SHUTDOWN
```

The splash polls immutable manager snapshots and maps the visible states to
`Update <version> available`, `Downloading… <percentage>%`, `Verifying…`,
`Update ready`, and `Download failed` with a separate `Retry` action.
While a splash window is visible, it is the foreground update surface and
suppresses duplicate desktop notifications for update progress or completion.
If a download finishes after that surface closes, desktop notifications remain
available for background completion or failure.
Neither the viewer nor streaming code depends on the update manager. Opening a
map closes only that splash instance, so an active download continues and a
later splash presents its current state. Closing the whole app moves the
manager to `SHUTDOWN`, cancels any active transfer, waits for its worker, and
removes the temporary staging directory.

A verified package is moved to `~/Downloads` and is never executed or
installed. A visible splash makes one automatic reveal attempt and retains a
manual platform action: macOS mounts a DMG read-only and shows its `.app` in
Finder, Windows selects the package in Explorer, and Linux asks the desktop
portal to reveal it, with `xdg-open` as a fallback. Completion while a map is
open remains silent until the splash is shown again.

On Linux, every directory chooser also uses XDG Desktop Portal when available.
Cancellation is distinct from portal failure; unavailable or old portals fall
back to the owned Tk chooser. Portal-selected source folders need only be
readable because generated cache assets are written to CaveViewer's cache root.

Default update checks read committed main-branch manifests, not GitHub's
latest-release or prerelease metadata. macOS uses
`updates/macos/<arm64|x86_64>/stable.json` and selects the running process
architecture, so a Rosetta-launched x86_64 build continues on the Intel update
channel. Linux distribution is x86_64-only and reads
`updates/linux/x86_64/stable.json`; Linux ARM64 builds do not receive automatic
updates. Stable publish runs update and sign `stable.json`.
Prerelease publish runs update the separate `prerelease.json` channel, leaving
`stable.json` unchanged, and mark a newly created GitHub release as a
prerelease. Uploading to an existing tag does not change that tag's
prerelease/latest status. For debugging, explicit environment variables can
point a source run or packaged app launched from Terminal at another branch or
manifest URL.

Prerelease branch testing can use the derived prerelease manifest URL after the
selected branch contains the matching platform manifest and signature. For
macOS, confirm the branch contains both files for the process architecture:

```bash
git ls-tree -r release/<version> updates/macos
```

The output must include:

```text
updates/macos/<arm64|x86_64>/prerelease.json
updates/macos/<arm64|x86_64>/prerelease.json.sig
```

Then test from a source checkout with `--update-branch` and `--force-update`:

```bash
CAVEVIEWER_UPDATE_CHANNEL=prerelease \
./run_caveviewer.sh --update-branch release/<version> --force-update
```

For a packaged app launched from Terminal, use environment variables instead:

```bash
CAVEVIEWER_FORCE_UPDATE=1 \
CAVEVIEWER_UPDATE_BRANCH=release/<version> \
CAVEVIEWER_UPDATE_CHANNEL=prerelease \
./CaveViewer-<version>-x86_64.AppImage
```

If the update checker logs `Update manifest fetch failed with HTTP 404`, the
derived branch/channel/platform manifest URL does not exist. Either publish that
platform's prerelease manifest to the selected branch, switch to a branch that
has it, or use `CAVEVIEWER_UPDATE_CHANNEL=stable` if you meant to test the
stable manifest.

If the update checker logs `Update manifest fetch failed with HTTP 429`, GitHub
has rate-limited the unauthenticated `raw.githubusercontent.com` request. The
splash interface stays unchanged. Wait for the limit to clear, switch networks,
or set `CAVEVIEWER_UPDATE_MANIFEST_URL` to a staging/custom-hosted copy of the
manifest; the signature URL defaults to `<manifest-url>.sig` unless
`CAVEVIEWER_UPDATE_MANIFEST_SIGNATURE_URL` is set explicitly.

Linux manifests are x86_64-only:

```text
updates/linux/x86_64/stable.json
updates/linux/x86_64/prerelease.json
```

macOS manifests are also architecture-specific:

```text
updates/macos/arm64/stable.json
updates/macos/arm64/prerelease.json
updates/macos/x86_64/stable.json
updates/macos/x86_64/prerelease.json
```

The `x86_64` files appear after the corresponding Intel channel is first
published. Top-level `updates/macos/stable.json` and `prerelease.json` files are
legacy ARM64 aliases. Keep each alias and signature byte-for-byte identical to
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
`CAVEVIEWER_RELEASE_SIGNING_PRIVATE_KEY` before publishing either channel. When
signing manually, either set that variable or pass `--private-key`.

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

| Variable | Default | Description |
|---|---|---|
| `CAVEVIEWER_UI_TEXT_SCALE` | `1.28` | Development override for adaptable in-app overlay text such as loading screens, controls/help overlay, and status readouts. Right-side viewer controls use `CAVEVIEWER_VIEWER_UI_SCALE` instead so labels and button geometry stay matched. |
| `CAVEVIEWER_VIEWER_UI_SCALE` | `auto` | Development override for the always-visible viewer HUD scale. By default CaveViewer keeps the compact baseline at 1536x864 and grows the right-side controls on larger viewer surfaces. |
| `CAVEVIEWER_TK_SCALE` | _(display DPI)_ | Windows/Linux override for Tk dialog scaling, clamped to `0.75` through `4.0`. The Linux AppImage launcher normally derives this value from the desktop Xft DPI setting. |
| `CAVEVIEWER_APPRUN_INSTALL_ONLY` | `0` | Linux AppImage launcher smoke mode. Set to `1` to install/update the desktop file, AppStream metadata, and hicolor icons in the user's XDG data home, print the installed paths, and exit without launching the GUI. |
| `CAVEVIEWER_APPRUN_UNINSTALL` | `0` | Linux AppImage launcher uninstall mode. Set to `1` to remove CaveViewer's per-user desktop file, AppStream metadata, and hicolor icons, then exit without launching the GUI. It does not remove maps, settings, caches, or downloaded update packages. |
| `CAVEVIEWER_NO_DESKTOP_INTEGRATION` | `0` | Linux AppImage launcher opt-out. Set to `1` to skip the best-effort per-user desktop integration step. |
| `CAVEVIEWER_UI_FONT` | _(platform default)_ | Absolute path to a `.ttf`/`.otf`/`.ttc` font file for the in-app FreeType renderer. Overrides the platform font search order. |
| `CAVEVIEWER_TEXT_AA_MODE` | `light` (macOS/Linux), `normal` (others) | FreeType anti-aliasing mode for in-app text. `normal` = standard hinting; `light` = smooth light anti-aliasing; `lcd` = LCD sub-pixel rendering. |
| `CAVEVIEWER_VSYNC` | `1` | Set to `0` to disable vertical sync. Recommended for virtual machines where the virtual display driver can block `swap_buffers()` long enough to freeze the render thread during heavy imports, making the window appear hung. |
| `CAVEVIEWER_WINDOW_SYSTEM` | `auto` | Linux viewer backend: `auto` prefers X11/XWayland when `DISPLAY` is available so source and AppImage launches get the same GNOME titlebar and resize behavior, then retries Wayland on recognized initialization failures. `wayland` and `x11` require that protocol without fallback. |
| `LIBGL_ALWAYS_SOFTWARE` | _(unset)_ | Linux OpenGL/Mesa setting. Set to `1` to force software rendering when a VM or GPU driver crashes, freezes, or leaves the app stuck in the graphics driver. |
| `CAVEVIEWER_NAVIGATION_GUARD` | `1` | Set to `0` to disable the navigation boundary that keeps free-fly movement near occupied map chunks. |
| `CAVEVIEWER_NAVIGATION_GUARD_RADIUS_CELLS` | `2` | Number of chunk cells around occupied map chunks that remain navigable. Larger values allow more free space around the cave; smaller values keep users closer to rendered chunks. |
| `CAVEVIEWER_FFMPEG` | _(auto)_ | Path to an `ffmpeg` executable for MP4 recording. If unset, CaveViewer tries system `ffmpeg`, then the bundled `imageio-ffmpeg` executable. |
| `CAVEVIEWER_RECORDING_DIR` | `~/Movies/CaveViewer` | Folder where saved recordings are stored. The Preferences panel saves this value. |
| `CAVEVIEWER_RECORDING_FPS` | `30` | Target MP4 recording frame rate. Range: 1–60. Frames are streamed to `ffmpeg`; they are not buffered in memory. |
| `CAVEVIEWER_RECORDING_MAX_HEIGHT` | `1080` | Maximum output video height. The framebuffer is downscaled before encoding to keep MP4 playback smooth. |
| `CAVEVIEWER_RECORDING_CRF` | `23` | H.264 quality value passed to `ffmpeg`. Lower is larger/higher quality; higher is smaller/lower quality. Range: 0–51. |

### Streaming Performance

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
empty field. Cancel, Escape, and window close remain available and discard
unapplied edits. Valid worker counts are treated as advisory caps and do not
show a bottom warning.

The Preferences implementation is split by responsibility:
`src/caveviewer/gui/preferences.py` owns the typed `SettingSpec` schema,
validation, persistence, and environment mapping;
`src/caveviewer/gui/preference_paths.py` owns preference/state file locations,
legacy migration, and atomic text writes;
`src/caveviewer/gui/advanced_settings_form.py` owns focus/change/blur/apply
state transitions; `src/caveviewer/gui/advanced_settings_dialog.py` only
renders that state into Tk widgets; and
`src/caveviewer/core/worker_config.py` resolves the effective streaming/import
worker counts while honoring reserved logical CPUs and owns the shared RAM
admission cutoff. Both pools start with one worker and admit at most one more
after completed work has been measured; they stay at their current concurrency
when system RAM utilization reaches 80% or current availability cannot be
measured. Only immutable, validated
`AdvancedSettings` snapshots may cross into
persistence or the runtime environment. Invalid saved or environment values
fall back independently to that field's valid default, so one stale value does
not discard the rest of the configuration. Settings are saved through an
atomic temporary-file replacement; a write failure remains visible in the
dialog and does not close it or alter the previous settings file.

First-time map imports are isolated from the viewer event loop by
`src/caveviewer/gui/import_process.py`. The viewer process keeps OpenGL/window
events and progress rendering responsive while a spawned child process runs the
existing `import_and_cache_any()` path. The child sends progress, completion,
heartbeat, and traceback-bearing failure events back to the viewer; closing
the viewer terminates an active child import process and removes abandoned
private staging directories for that map cache. The child also lowers its
desktop scheduling priority and caps common native compute libraries to one
thread unless the user has already set those library-specific variables.

The splash screen, Preferences, and Sample Maps dialogs share their Tk
color and control tokens through `src/caveviewer/gui/tk_theme.py`. Map-folder
validation lives in `src/caveviewer/gui/map_selection.py`, allowing both
map-selection dialogs to reuse it without importing private splash-screen
implementation details.

Runtime chunk streaming is also split by policy boundary:
`src/caveviewer/core/hardware_memory.py` detects total and currently available
system RAM on Windows, macOS, and Linux; detects or estimates the active GPU
memory budget; and parses target fractions;
`src/caveviewer/core/streaming_budget.py` contains pure
chunk-size estimation and residency-cap calculation;
`src/caveviewer/core/streaming_scheduler.py` owns the bounded ready backlog,
spatial selection, and eviction policy; and
`src/caveviewer/core/streaming_world.py` coordinates worker lifecycle and
render-thread callbacks. Map imports now write only the cache artifacts used by
runtime streaming and the minimap. Caches and their texture assets are
atomically published under the managed map-cache root selected by
`CAVEVIEWER_MAP_CACHE_DIR` or the platform cache default; old adjacent `_cache`
and `.caveviewer_cache` directories are not auto-discovered.

| Variable | Default | Accepted range | Description |
|---|---|---|---|
| `CAVEVIEWER_MEMORY_UTILIZATION_TARGET` | `8` | 1-80% | Percentage of system RAM the chunk streaming system targets for loaded chunk data. |
| `CAVEVIEWER_GPU_MEMORY_GB` | _(auto-detect)_ | 0.5-50 GB (optional) | Optional GPU memory ceiling used by the streaming budget. Linux AMD GPUs are detected through DRM sysfs and NVIDIA GPUs through `nvidia-smi`; low-VRAM AMD integrated GPUs include 50% of reported GTT/shared memory capped at 2 GB. Windows AMD/Intel GPU memory is not currently auto-detected and uses an 8 GB fallback budget. macOS GPU memory is not currently auto-detected and uses a conservative 1 GB fallback. If detection finds a smaller active GPU budget, the detected value wins. |
| `CAVEVIEWER_GPU_MEMORY_UTILIZATION_TARGET` | `70` | 1-80% | Percentage of the GPU memory budget the chunk streaming system targets. |
| `CAVEVIEWER_MAX_TEXTURE_SIZE` | _(auto)_ | 512-16384 px | Optional maximum decoded texture dimension. When unset, CaveViewer derives a cap from GPU memory, GPU target percentage, and unique texture count so geometry remains visible while oversized texture sets are downscaled instead of overfilling VRAM. The log records the selected cap, budget inputs, and first actual resize. |
| `CAVEVIEWER_IO_WORKERS` | `2` | Integer 1-32 workers | Requested maximum number of background threads for loading chunk files from disk. Streaming starts one worker and grows one at a time after completed chunk work, provided system RAM utilization remains below 80%. If availability cannot be measured, it remains at one. The runtime also honors `CAVEVIEWER_IO_RESERVED_CPUS`. |
| `CAVEVIEWER_IO_RESERVED_CPUS` | `3` | Integer 2-32 logical CPUs | Logical CPUs kept out of the loading worker pool. Effective workers are capped at `logical CPUs - reserved CPUs`, with at least one worker. |
| `CAVEVIEWER_UPLOAD_CHUNKS_PER_FRAME` | `1` | 1-16 chunks | Maximum number of chunk GPU uploads per render frame. Increase to load geometry faster at the cost of brief frame-time spikes. |
| `CAVEVIEWER_UPLOAD_TIME_BUDGET_MS` | `3.0` | 0.5-50 ms | Target milliseconds spent uploading chunks to the GPU each frame. |

### Map Import (First-Time Parsing)

| Variable | Default | Accepted range | Description |
|---|---|---|---|
| `CAVEVIEWER_CHUNK_SIZE_METERS` | `50` | 0.01-512 | Unitless chunk edge length used when building a new chunk cache. Does not affect already-cached maps. |
| `CAVEVIEWER_OBJ_SCAN_THROTTLE_MS` | `1` (Windows), `0` (others) | 0-50 ms | Milliseconds paused while scanning .obj files. A small value keeps the UI responsive during large imports on Windows; `0` disables throttling. |
| `CAVEVIEWER_OBJ_IMPORT_BATCH_FACES` | `200000` | Integer 1,000-2,000,000 | Number of triangulated OBJ faces processed per incremental import batch. Preferences display this as “Faces per .obj batch” in thousands, default 200 with accepted range 1-2,000 thousand faces. |
| `CAVEVIEWER_OBJ_BUCKET_WORKERS` | `2` | Integer 1-32 workers | Worker threads used to de-index, group, and write incremental .obj face batches into temporary bucket parts. Increase on SSDs to reduce import time at the cost of extra transient RAM and higher temporary-file I/O. |
| `CAVEVIEWER_IMPORT_NICE` | `5` | Integer >= 0 | POSIX-only nice increment applied to the spawned import child. `0` disables the adjustment. Windows uses below-normal process priority instead. |
| `CAVEVIEWER_CHUNK_BUILD_WORKERS` | `1` | Integer 1-32 workers | Requested maximum threads used by the in-memory cache builder while writing chunk files. Cache construction starts one worker and grows one at a time after completed chunk work, provided system RAM utilization remains below 80%. If availability cannot be measured, it remains at one. Incremental OBJ batch bucketing is controlled separately by `CAVEVIEWER_OBJ_BUCKET_WORKERS`, then finalized sequentially into chunk files. The runtime also honors `CAVEVIEWER_CHUNK_BUILD_RESERVED_CPUS`. |
| `CAVEVIEWER_CHUNK_BUILD_RESERVED_CPUS` | `2` | Integer 2-32 logical CPUs | Logical CPUs kept out of the cache-building worker pool. Effective workers are capped at `logical CPUs - reserved CPUs`, with at least one worker. |

### Command-line cache compilation

`caveviewer-chunker` compiles or rebuilds managed map caches without launching
the GUI. It is intended for developers and advanced users who want to run map
imports from a shell, CI job, cron/systemd timer, or benchmark workflow.

```bash
caveviewer-chunker --source=/path/to/map-or-folder --chunk-size=64
```

From a source checkout, the module entry point is equivalent:

```bash
.venv-dev/bin/python -m caveviewer.chunker \
  --source=/path/to/map-or-folder \
  --chunk-size=64
```

Windows PowerShell examples:

```powershell
caveviewer-chunker --source="C:\Maps\Peacock.obj" --chunk-size=64
```

From a source checkout on Windows, use the development virtual environment's
Python executable:

```powershell
.\.venv-dev\Scripts\python.exe -m caveviewer.chunker `
  --source="C:\Maps\Peacock.obj" `
  --chunk-size=64
```

Use `--cache-root` when compiled maps should live on a specific drive:

```powershell
.\.venv-dev\Scripts\python.exe -m caveviewer.chunker `
  --source="C:\Maps\Peacock.obj" `
  --cache-root="D:\CaveViewer\maps" `
  --chunk-size=64 `
  --json
```

To have the GUI auto-discover that cache later, launch CaveViewer from a shell
with the same cache root:

```powershell
$env:CAVEVIEWER_MAP_CACHE_DIR = "D:\CaveViewer\maps"
caveviewer
```

The PowerShell environment assignment applies to the current shell session.
Set it again in new shells, or configure a persistent user environment variable
if this cache root should be reused regularly.

The command follows the same public CLI conventions as the shell scripts in
`scripts/STANDARDS.md`: named options only, `-h`/`--help`, compact usage text,
and exact errors for positional arguments, unknown options, and missing option
values. Run the command with `--help` to see the latest supported options,
defaults, and examples:

```bash
caveviewer-chunker --help
```

From a source checkout:

```bash
.venv-dev/bin/python -m caveviewer.chunker --help
```

From a Windows source checkout:

```powershell
.\.venv-dev\Scripts\python.exe -m caveviewer.chunker --help
```

Normal GUI-compatible output does not require `--cache-root`; CaveViewer uses
the platform managed map-cache root. Use `--cache-root` when compiled maps
should live in a specific root folder. For example, this command:

```bash
caveviewer-chunker \
  --source=/maps/Peacock.obj \
  --cache-root=/data/caveviewer/maps \
  --chunk-size=64
```

writes a cache similar to:

```text
/data/caveviewer/maps/Peacock-<source-path-hash>/
```

The GUI must use the same cache root to auto-discover that cache when opening
the source map:

```bash
CAVEVIEWER_MAP_CACHE_DIR=/data/caveviewer/maps caveviewer
```

If the existing cache is valid and its manifest chunk size matches the
requested chunk size, the command skips the import. If the existing cache is
valid but its manifest chunk size differs, the command rebuilds automatically.
`--force` is only needed to rebuild an already-matching cache.

### Application storage locations

Unless overridden, CaveViewer stores files in these locations:

| Kind | Linux default | macOS/Windows default |
|---|---|---|
| Advanced settings | `$XDG_CONFIG_HOME/caveviewer/advanced_settings.json` (`~/.config/...` fallback) | `~/.caveviewer/advanced_settings.json` |
| Remembered chooser locations | `$XDG_STATE_HOME/caveviewer/` (`~/.local/state/...` fallback) | `~/.caveviewer/` |
| Map caches | `$XDG_CACHE_HOME/caveviewer/maps/` (`~/.cache/...` fallback) | `~/.caveviewer/maps/` |

`CAVEVIEWER_HOME` creates isolated `config`, `data`, `cache`, `state`, and
`runtime` children and places map caches under `<root>/cache/maps`.
`CAVEVIEWER_MAP_CACHE_DIR` overrides only the generated map-cache root. On
Linux, old `~/.caveviewer/` and `~/.caveviewer_*` files are copied once and
left in place. A managed cache is self-contained: texture files are staged
beside its chunks before the manifest becomes visible. Disk-space checks
therefore target the cache filesystem rather than assuming the map's filesystem
is writable.

### Sample Maps

| Variable | Default | Description |
|---|---|---|
| `CAVEVIEWER_SAMPLE_MAPS_REPO` | `KernalPanic/CaveViewer` | GitHub `owner/repo` for the sample maps release. |
| `CAVEVIEWER_SAMPLE_DATA_TAG` | `sample-data` | Release tag to fetch sample map assets from. |
| `CAVEVIEWER_SAMPLE_MAPS_API_URL` | _(derived from repo + tag)_ | Full GitHub release API URL. Overrides the repo/tag variables when set. |
