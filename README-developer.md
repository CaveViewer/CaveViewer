# CaveViewer Development From Source

This guide is for users who want to run CaveViewer from source.

Scope:

- This document is intentionally focused on source-based development and local runs.

## Get Source Files

You can start in either of these ways:

- Clone the repository with Git (recommended for contributors).
- Download source files from GitHub and unpack them locally.

The repository's source archive format is:

- `CaveViewer-<version>-source.tar.gz`

This format is produced by the existing source packaging flow in `scripts/common/package_source.sh`.

Release packages should include:

- `LICENSE`
- `THIRD_PARTY_NOTICES.md`

The application About text should identify CaveViewer as licensed under the GNU General Public License version 3.0.

## Requirements

- Git
- Python 3.10+

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
- Generates `run_caveviewer.sh`

Run the app:

```bash
./run_caveviewer.sh
```

Alternatively:

```bash
# If you activated the virtual environment, run the 'source' line below
source .venv-dev/bin/activate
.venv-dev/bin/python ./caveviewer.py
```

## Windows: Run From Source

Option A (recommended for technical users): manual venv flow.

```powershell
py -3 -m venv .venv-dev
.\.venv-dev\Scripts\python -m pip install --upgrade pip
.\.venv-dev\Scripts\python -m pip install -r requirements.txt
.\.venv-dev\Scripts\python caveviewer.py
```

Option B (guided setup script in this repo):

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\windows\setup.ps1
```

Notes:

- `scripts/windows/setup.ps1` is designed to install prerequisites and set up a runnable local source environment.
- `scripts/windows/launch.bat` is a launcher for the setup script.

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
.\.venv-dev\Scripts\python caveviewer.py
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
| `CAVEVIEWER_HOME` | _(none)_ | Override the home directory CaveViewer uses for preferences and cache files. |
| `CAVEVIEWER_APP_ICON` | _(bundled icon)_ | Path to a custom application icon file. |
| `CAVEVIEWER_FORCE_STARTUP_FOCUS` | `0` | Set to `1` to force the main window to the front on startup. Disabled by default on frozen macOS builds to avoid window-placement jumps. |
| `CAVEVIEWER_LOG_LEVEL` | `INFO` | Logging verbosity. Accepted values: `DEBUG`, `INFO`, `WARNING`, `ERROR`. |

### Update Checking

| Variable | Default | Description |
|---|---|---|
| `CAVEVIEWER_GITHUB_REPO` | `KernalPanic/CaveViewer` | The GitHub `owner/repo` used to build the default update manifest URL and sample-maps API URL. Override when running a fork or testing a package from Terminal. |
| `CAVEVIEWER_UPDATE_BRANCH` | `main` | Git branch used when deriving the default `raw.githubusercontent.com` update manifest URL. Also available as `--update-branch <branch>` for updater testing from a non-`main` branch. Ignored when `CAVEVIEWER_UPDATE_MANIFEST_URL` is set. |
| `CAVEVIEWER_UPDATE_CHANNEL` | `stable` | Update manifest channel used when deriving the default manifest URL. Accepted values: `stable`, `prerelease`. Ignored when `CAVEVIEWER_UPDATE_MANIFEST_URL` is set. |
| `CAVEVIEWER_UPDATE_MANIFEST_URL` | _(derived from repo)_ | Full URL to the JSON update manifest. Overrides the default `raw.githubusercontent.com` path. Useful for pointing at a staging manifest or a custom server. |
| `CAVEVIEWER_UPDATE_MANIFEST_SIGNATURE_URL` | `<manifest-url>.sig` | Full URL to the base64 Ed25519 signature for the update manifest. |
| `CAVEVIEWER_FORCE_UPDATE` | `0` | Set to `1` (or `true`/`yes`) to always show the "Download Update" prompt regardless of the manifest version. Also available as `--force-update`. For testing the update UI without waiting for the CDN cache or changing version numbers. |
| `CAVEVIEWER_LINUX_UPDATE_ARCH` | _(auto)_ | Linux publish helper only. Set to `arm64` or `x86_64` to choose which AppImage is written to the Linux update manifest. |

Update manifests are signed with the release Ed25519 private key. The bundled
public key lives at `security/release_signing_public_key.pem`. Startup update
checks read the branch/channel manifest first; if it advertises a newer version,
the app verifies the manifest signature before offering the download. Missing or
invalid signatures are logged as errors and do not change the splash interface.

Default update checks read the committed main-branch
`updates/<platform>/stable.json` manifests, not GitHub's latest-release or
prerelease metadata. Stable publish runs update and sign `stable.json`.
Prerelease publish runs mark the GitHub release as a prerelease and update the
separate `prerelease.json` channel, leaving `stable.json` unchanged. For
debugging, explicit environment variables can point a source run or packaged
app launched from Terminal at another branch or manifest URL.

Prerelease branch testing can use the derived prerelease manifest URL after the
selected branch contains the matching platform manifest and signature. For
macOS, confirm the branch contains both files first:

```bash
git ls-tree -r release/<version> updates/macos
```

The output must include:

```text
updates/macos/prerelease.json
updates/macos/prerelease.json.sig
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
./CaveViewer-<version>-aarch64.AppImage
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

Linux manifests are architecture-specific:

```text
updates/linux/arm64/stable.json
updates/linux/arm64/prerelease.json
updates/linux/x86_64/stable.json
updates/linux/x86_64/prerelease.json
```

Sign a manifest:

```bash
python3 scripts/sign_update_manifest.py \
  updates/macos/stable.json \
  --private-key /path/to/release_signing_private_key.pem
```

This writes `updates/macos/stable.json.sig`. Release publish scripts do not use
a default private-key path; set `CAVEVIEWER_RELEASE_SIGNING_PRIVATE_KEY` before
running stable releases. When signing manually, either set that variable or
pass `--private-key`.

### UI & Rendering

#### OpenGL UI scaling

The OpenGL viewer renders its overlay text directly with FreeType in screen
pixels. It does not automatically inherit GNOME, KDE, X11, or Wayland desktop
scaling. On a high-DPI display, set `CAVEVIEWER_UI_TEXT_SCALE` when starting
CaveViewer. The built-in default is `1.28`; for example, applying an additional
150% scale gives `1.28 * 1.5 = 1.92`:

```bash
CAVEVIEWER_UI_TEXT_SCALE=1.92 ./run_caveviewer.sh
```

The accepted range is `0.5` through `3.0`. The controls/help overlay derives
its row height from the resulting FreeType line metrics, so increasing the text
scale also reserves enough vertical space for each line and its keycap. Text
inside the fixed-size right-side control panel (steppers and action buttons)
stays at its designed size because that panel's geometry does not scale.

To keep a development-machine override, export it in the shell profile or add
the following before the final `exec` in `run_caveviewer.sh`:

```bash
export CAVEVIEWER_UI_TEXT_SCALE="${CAVEVIEWER_UI_TEXT_SCALE:-1.92}"
```

`scripts/dev/install.sh` regenerates `run_caveviewer.sh`, so edits made only to
the generated launcher are replaced the next time the installer runs. For a
repeatable project-specific default, add the same export to the launcher
template in `scripts/dev/install.sh`; keep the `${...:-...}` form so callers can
still override it for an individual run.

| Variable | Default | Description |
|---|---|---|
| `CAVEVIEWER_UI_TEXT_SCALE` | `1.28` | Scale multiplier for adaptable in-app overlay text (loading screens, controls/help overlay, and HUD readouts). Text inside fixed-size control geometry is intentionally excluded. `1.0` is the base size. |
| `CAVEVIEWER_UI_FONT` | _(platform default)_ | Absolute path to a `.ttf`/`.otf`/`.ttc` font file for the in-app FreeType renderer. Overrides the platform font search order. |
| `CAVEVIEWER_TEXT_AA_MODE` | `light` (macOS), `normal` (others) | FreeType anti-aliasing mode for in-app text. `normal` = standard hinting; `light` = smooth light anti-aliasing (matches macOS CoreText style); `lcd` = LCD sub-pixel rendering. |
| `CAVEVIEWER_VSYNC` | `1` | Set to `0` to disable vertical sync. Recommended for virtual machines where the virtual display driver can block `swap_buffers()` long enough to freeze the render thread during heavy imports, making the window appear hung. |
| `LIBGL_ALWAYS_SOFTWARE` | _(unset)_ | Linux OpenGL/Mesa setting. Set to `1` to force software rendering when a VM or GPU driver crashes, freezes, or leaves the app stuck in the graphics driver. |
| `CAVEVIEWER_NAVIGATION_GUARD` | `1` | Set to `0` to disable the navigation boundary that keeps free-fly movement near occupied map chunks. |
| `CAVEVIEWER_NAVIGATION_GUARD_RADIUS_CELLS` | `2` | Number of chunk cells around occupied map chunks that remain navigable. Larger values allow more free space around the cave; smaller values keep users closer to rendered chunks. |
| `CAVEVIEWER_FFMPEG` | _(auto)_ | Path to an `ffmpeg` executable for MP4 recording. If unset, CaveViewer tries system `ffmpeg`, then the bundled `imageio-ffmpeg` executable. |
| `CAVEVIEWER_RECORDING_DIR` | `~/Movies/CaveViewer` | Directory where clean MP4 flight recordings are saved. The Advanced Settings panel saves this value. |
| `CAVEVIEWER_RECORDING_FPS` | `30` | Target MP4 recording frame rate. Range: 1–60. Frames are streamed to `ffmpeg`; they are not buffered in memory. |
| `CAVEVIEWER_RECORDING_MAX_HEIGHT` | `1080` | Maximum output video height. The framebuffer is downscaled before encoding to keep MP4 playback smooth. |
| `CAVEVIEWER_RECORDING_CRF` | `23` | H.264 quality value passed to `ffmpeg`. Lower is larger/higher quality; higher is smaller/lower quality. Range: 0–51. |

### Streaming Performance

| Variable | Default | Description |
|---|---|---|
| `CAVEVIEWER_MEMORY_UTILIZATION_TARGET` | `8` | Percentage of system RAM the chunk streaming system targets for loaded chunk data. |
| `CAVEVIEWER_GPU_MEMORY_GB` | _(auto-detect)_ | Override the GPU memory size (in GB) used by the streaming budget. Useful when auto-detection reports an incorrect value. |
| `CAVEVIEWER_GPU_MEMORY_UTILIZATION_TARGET` | `70` | Percentage of GPU memory the streaming system targets. |
| `CAVEVIEWER_IO_WORKERS` | `2` | Number of background threads for loading chunk files from disk. |
| `CAVEVIEWER_IO_RESERVED_CPUS` | `3` | CPU cores to keep free when computing a custom `CAVEVIEWER_IO_WORKERS` value. |
| `CAVEVIEWER_UPLOAD_CHUNKS_PER_FRAME` | `1` | Maximum number of chunk GPU uploads per render frame. Increase to load geometry faster at the cost of brief frame-time spikes. Range: 1–16. |
| `CAVEVIEWER_UPLOAD_TIME_BUDGET_MS` | `3.0` | Soft per-frame time budget (ms) for GPU uploads. Range: 0.5–50. |

### Map Import (First-Time Parsing)

| Variable | Default | Description |
|---|---|---|
| `CAVEVIEWER_CHUNK_SIZE_METERS` | `8` | Spatial chunk size in metres used when building a new chunk cache. Does not affect already-cached maps. |
| `CAVEVIEWER_OBJ_SCAN_THROTTLE_MS` | `1` (Windows), `0` (others) | Milliseconds to yield between OBJ scanning steps. A small value keeps the UI responsive during large imports on Windows; `0` disables throttling. |
| `CAVEVIEWER_CHUNK_BUILD_WORKERS` | `1` | Threads used while writing chunk files during import. |
| `CAVEVIEWER_CHUNK_BUILD_RESERVED_CPUS` | `2` | CPU cores to keep free when computing a custom `CAVEVIEWER_CHUNK_BUILD_WORKERS` value. |

### Sample Maps

| Variable | Default | Description |
|---|---|---|
| `CAVEVIEWER_SAMPLE_MAPS_REPO` | `KernalPanic/CaveViewer` | GitHub `owner/repo` for the sample maps release. |
| `CAVEVIEWER_SAMPLE_DATA_TAG` | `sample-data` | Release tag to fetch sample map assets from. |
| `CAVEVIEWER_SAMPLE_MAPS_API_URL` | _(derived from repo + tag)_ | Full GitHub release API URL. Overrides the repo/tag variables when set. |
