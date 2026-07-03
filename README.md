# CaveViewer

A standalone viewer for large cave-survey 3D mesh maps, built for maps too big to comfortably load all at once. Instead of loading the whole mesh into memory/VRAM, CaveViewer splits it into a 3D grid of spatial chunks and only keeps the chunks near your current position loaded -- so frame rate stays smooth no matter how big the full cave system is.

Supports three map input types:

- **OBJ** (+ matching `.mtl` + tiled `.jpg` textures) -- the original format this was built for, exported from Agisoft Metashape.

- **GLB** (binary glTF) -- including maps whose textures are embedded directly inside the file rather than as separate images.

- **Pre-compiled map cache** (`.caveviewer_cache` with `manifest.json`) -- launch directly from an already chunked map without re-importing OBJ/GLB.

Whichever format a map is in, the rest of the program (chunking, streaming, all the on-screen controls) behaves identically -- format only matters at the moment a map is first opened.

## How it works

1. **First time opening a map:** CaveViewer parses your `.obj` (streaming, so it doesn't choke on multi-GB files) and splits it into a grid of spatial chunks (default 8m cubes), writing a cache folder `.caveviewer_cache` next to your `.obj` file. This is a one-time cost -- for a 10-20 million triangle map, expect roughly 30-90 seconds depending on your CPU and disk speed.

2. **Every time after:** CaveViewer detects the existing cache and skips straight to launching the viewer -- near-instant.

3. **While flying around:** only the chunks within a few grid cells of your camera are loaded into GPU memory. As you fly deeper into the cave, far chunks behind you are unloaded and new chunks ahead of you stream in, automatically, in a background thread so it doesn't stall your frame rate.

## How to install and run CaveViewer

## Supported platforms

MacOS X - Apple Silicon
Linux - Fedora, Ubuntu
Windows - 10, 11

### macOS app (recommended)

Download the latest DMG from https://github.com/KernalPanic/CaveViewerPlus/releases, open it, and drag CaveViewer into Applications. If this is your first time installing the app, you have to go to Settings -> Privacy & Security and then allow the system to open CaveViewer. These steps are necessary because the app is not published through the App Store.

If there is an update, the app will let you download and install it.

NOTE: Intel Macs are not supported.

### Windows app (recommended)

Download the latest zip from https://github.com/KernalPanic/CaveViewerPlus/releases and extract it anywhere on your machine.

**First-time setup:** double-click `launch.bat` inside the extracted folder. This opens a guided setup window that will:

1. Install Python 3.12 if it is not already present (downloads from python.org and registers it on your PATH automatically — a UAC prompt will appear, which is expected).
2. Install/check the Visual C++ Redistributable needed by Python extension packages.
3. Install the required Python packages from `requirements.txt`.
4. Add a Windows Firewall outbound rule for Python so update checks and sample-map downloads can reach GitHub.
5. Create a Desktop shortcut so you can launch CaveViewer with a double-click going forward.

> **Note:** `launch.bat` exists because Windows blocks `.ps1` files from running when double-clicked. The bat file launches the bundled `setup.ps1` with the correct flags — it does not change any permanent PowerShell execution-policy setting.

After setup completes the window closes automatically. Use the Desktop shortcut (or re-run `launch.bat`) to start CaveViewer.

If there is an update, the app will let you download and install it.

The update manifests are platform-specific, so the macOS app reads `updates/macos/stable.json`, the Windows app reads `updates/windows/stable.json`, and the Linux app reads `updates/linux/stable.json`.

### Linux app (AppImage) - Ubuntu or Fedora

The best-practice distribution format for Linux is the self-contained AppImage — a single executable file that bundles Python, all dependencies, and the app itself. No system-wide installation or package manager involvement required.

**1. Download the AppImage**

Download the AppImage matching your CPU from https://github.com/KernalPanic/CaveViewerPlus/releases:

- `CaveViewer-<version>-x86_64.AppImage` (amd64 / x86_64)
- `CaveViewer-<version>-aarch64.AppImage` (arm64)

**2. Make it executable and run**

```bash
chmod +x CaveViewer-*.AppImage

# Run the one that matches your architecture
./CaveViewer-*-x86_64.AppImage   # amd64 / x86_64
# or
./CaveViewer-*-aarch64.AppImage  # arm64
```

The AppImage self-extracts to a temporary directory on launch and cleans up when it exits. No installation step is needed.

**System requirements:** a display server (X11 or Wayland with XWayland), OpenGL 3.3+, and the following runtime libraries which are present by default on most desktop distributions:

```bash
# Ubuntu/Debian — install if the app fails to start
sudo apt-get install libfreetype6 libgl1-mesa-dri libxkbcommon0
```

If there is an update, the app will let you download and install it.

### From source (development)

For development, clone the repository and run from the working tree. Requires Python 3.10+.

**1. Clone and check out the latest stable tag**

```bash
git clone https://github.com/KernalPanic/CaveViewerPlus.git
cd CaveViewerPlus
git fetch --tags
latest=$(git tag -l "v*" --sort=-version:refname | head -n 1)
git checkout "$latest"
```

**2. Create a development virtual environment and install dependencies**

Install typical dependencies

```bash
sudo apt update
sudo apt install -y \
  build-essential wget libssl-dev zlib1g-dev libbz2-dev \
  libreadline-dev libsqlite3-dev libncursesw5-dev xz-utils \
  tk-dev libffi-dev liblzma-dev python3-tk tk-dev tcl-dev libgl1 libegl1 libglx-mesa0 
```  
`scripts/dev/install.sh` creates a dedicated development virtual environment at `.venv-dev` (or uses `CAVEVIEWER_DEV_VENV` if set), installs all packages from `requirements.txt`, and generates a `run_caveviewer.sh` launcher.

Linux packaging/build scripts use a separate dedicated virtual environment (`.venv-linux-build-<arch>` by default, or `CAVEVIEWER_LINUX_BUILD_VENV` when set) so build dependencies stay isolated from your development environment:

```bash
./scripts/dev/install.sh
```

**3. (Optional) Configure environment variables**

`scripts/dev/env_setup.sh` sets `PYTHONPATH`, `CAVEVIEWER_HOME`, and `CAVEVIEWER_GITHUB_REPO` for the current shell session. Source it (don't execute it) so the variables are exported to your shell:

```bash
source ./scripts/dev/env_setup.sh
```

You can also override the update manifest URL by uncommenting and setting `CAVEVIEWER_UPDATE_MANIFEST_URL` inside that file.

For the complete list of supported environment variables (runtime, update, and build/packaging), see the Environment Variables section below.

**4. Run**

```bash
./run_caveviewer.sh
```

## Controls

| Input              | Action                          |
|---------------------|----------------------------------|
| `W` / `S`           | Fly forward / backward          |
| `A` / `D`           | Strafe left / right             |
| `E` / `Q`           | Move up / down                  |
| Right-click + mouse (macOS) / Left-click + mouse (Windows/Linux) | Look around (yaw/pitch) |
| `Option` + left-click + mouse (macOS) | Look around (yaw/pitch) |
| `← ↑ ↓ →`           | Look around (yaw/pitch)     |
| `I` / `J` / `K` / `L`   | Look around (yaw/pitch)     |
| `Z` / `X`           | Barrel roll (counterclockwise / clockwise) |
| `Cmd` + `0` (macOS) / `Ctrl` + `0` (Windows/Linux) | Reset view (level horizon) |
| `Cmd` + `1`..`9` (macOS) / `Ctrl` + `1`..`9` (Windows/Linux) | Save camera bookmark to slot 1..9 |
| `1`..`9` | Recall camera bookmark from slot 1..9 |
| `Esc`               | Quit                            |
| `Shift` (held)      | 3x speed boost                  |


## Creating a Release

Releases are managed through `scripts/release.sh`, which dispatches to platform-specific scripts.

### Recommended: unified all-platform flow

```bash
# Build packages for supported platforms from one command
./scripts/release.sh all-package --version=1.2.3

# Build and publish in one command (reuses existing artifacts when present)
./scripts/release.sh all-package --version=1.2.3 --publish --release-notes="Bug fixes and stability improvements"
```

Notes:

- `--version` is required for `all-package`.
- Linux architecture defaults to `both` (`amd64` + `arm64`), override with `--linux-arch=amd64|arm64|both`.
- Use `--rebuild` to force fresh artifacts before publish.
- Use `--skip=macos,linux,windows` to skip targets.

### macOS

```bash
# Build the DMG
./scripts/release.sh macos-package

# Publish to GitHub and update the macOS update manifest
./scripts/release.sh macos-publish <version> "Release notes"

# Publish already-built artifacts without rebuilding
./scripts/release.sh macos-publish --skip-build <version> "Release notes"

# Example
./scripts/release.sh macos-publish 1.2.3 "Bug fixes and stability improvements"
```

### Windows

```bash
# Build the zip
./scripts/release.sh windows-package

# Publish to GitHub and update the Windows update manifest
./scripts/release.sh windows-publish <version> "Release notes"

# Publish already-built artifacts without rebuilding
./scripts/release.sh windows-publish --skip-build <version> "Release notes"

# Example
./scripts/release.sh windows-publish 1.2.3 "Bug fixes and stability improvements"
```

### Linux

Direct Linux builds must be created on a Linux host. The unified `all-package` flow can also build Linux artifacts from macOS when Docker is available.

```bash
# Build the AppImage (must run on Linux)
./scripts/release.sh linux-package

# Publish to GitHub and update the Linux update manifest
./scripts/release.sh linux-publish <version> "Release notes"

# Publish already-built artifacts without rebuilding
./scripts/release.sh linux-publish --skip-build <version> "Release notes"

# Example
./scripts/release.sh linux-publish 1.2.3 "Bug fixes and stability improvements"
```

Publish scripts create or update the GitHub release tag (`v<version>`), upload assets, and write the corresponding update manifest (`updates/macos/stable.json`, `updates/windows/stable.json`, or `updates/linux/stable.json`) so the in-app updater picks up the new version.


## Environment Variables

You can configure CaveViewer behavior through environment variables without editing code.

### Runtime performance and UI

- `CAVEVIEWER_CHUNK_SIZE_METERS`
  Chunk size used when building a new cache. Default: `8.0`.
  For very large maps, try `16` or `24`.

- `CAVEVIEWER_MEMORY_UTILIZATION_TARGET`
  Target RAM share used to derive runtime chunk residency (`max_loaded_chunks`).
  Conservative default: `12%` of detected physical RAM.
  Accepts either fraction (`0.12`) or percent-style (`12`, `25`).
  This is not an absolute GB value.
  Uses total physical RAM as a reference (not currently free RAM), and
  acts as a residency policy target rather than an OS-level reservation.
  Other running applications can still reduce effectively available memory.

- `CAVEVIEWER_IO_WORKERS`
  Runtime chunk-load worker thread count.
  Recommended starting range for very large maps on Windows: `2` to `6` (try `4` first).

- `CAVEVIEWER_IO_RESERVED_CPUS`
  CPU cores to reserve when `CAVEVIEWER_IO_WORKERS` is not explicitly set.

- `CAVEVIEWER_UPLOAD_CHUNKS_PER_FRAME`
  Maximum number of ready chunks to upload to the GPU per frame. Default: `1`.
  Lower values favor smooth frame pacing; higher values can catch up faster after large teleports.

- `CAVEVIEWER_UPLOAD_TIME_BUDGET_MS`
  Soft per-frame time budget for uploading ready chunks. Default: `3.0`.
  A single large chunk can still exceed this because upload work cannot be interrupted mid-chunk.
  This setting controls runtime streaming/upload pacing only; it does not affect initial cache import.

- `CAVEVIEWER_CHUNK_BUILD_WORKERS`
  Worker thread count for writing chunk files during cache build/import.

- `CAVEVIEWER_CHUNK_BUILD_RESERVED_CPUS`
  CPU cores to reserve for cache build worker auto-sizing.

Runtime tuning examples:

```bash
# macOS / Linux: large-map runtime tuning
export CAVEVIEWER_CHUNK_SIZE_METERS=16
export CAVEVIEWER_MEMORY_UTILIZATION_TARGET=20
export CAVEVIEWER_IO_WORKERS=4
export CAVEVIEWER_UPLOAD_CHUNKS_PER_FRAME=1
export CAVEVIEWER_UPLOAD_TIME_BUDGET_MS=3.0
./run_caveviewer.sh
```

```powershell
# Windows PowerShell: large-map runtime tuning
$env:CAVEVIEWER_CHUNK_SIZE_METERS = "16"
$env:CAVEVIEWER_MEMORY_UTILIZATION_TARGET = "20"
$env:CAVEVIEWER_IO_WORKERS = "4"
$env:CAVEVIEWER_UPLOAD_CHUNKS_PER_FRAME = "1"
$env:CAVEVIEWER_UPLOAD_TIME_BUDGET_MS = "3.0"
python caveviewer.py
```

Powerful workstation starting point (Windows 11, high-core-count CPU, 128GB RAM, NVIDIA GPU with 24GB VRAM):

```powershell
$env:CAVEVIEWER_CHUNK_SIZE_METERS = "32"
$env:CAVEVIEWER_MEMORY_UTILIZATION_TARGET = "25"
$env:CAVEVIEWER_IO_WORKERS = "4"
$env:CAVEVIEWER_UPLOAD_CHUNKS_PER_FRAME = "1"
$env:CAVEVIEWER_UPLOAD_TIME_BUDGET_MS = "3.0"
python caveviewer.py
```

In the viewer, start with **DISTANCE** set to `1` or `2`. If the map is already cached, only the runtime settings (`MEMORY_UTILIZATION_TARGET`, `IO_WORKERS`, and upload pacing) take effect immediately. Changing `CAVEVIEWER_CHUNK_SIZE_METERS` requires deleting/rebuilding the map's `.caveviewer_cache`, because chunk size is baked into the cache.

To check the chunk size of a precomputed map, open its `.caveviewer_cache/manifest.json` and look for:

```json
"chunk_size": 32.0
```

When opening an existing or precomputed cache, CaveViewer always uses the `chunk_size` recorded in `manifest.json`, even if `CAVEVIEWER_CHUNK_SIZE_METERS` is currently set to a different value. The environment variable only controls new cache imports or rebuilt caches.

If Windows streaming appears I/O-bound on very large maps, test these combinations in order:

1. `32m` chunks, **DISTANCE** `1`, `IO_WORKERS=4`
2. `32m` chunks, **DISTANCE** `2`, `IO_WORKERS=4`
3. `48m` chunks, **DISTANCE** `1`, `IO_WORKERS=2`
4. `48m` chunks, **DISTANCE** `1`, `IO_WORKERS=4`

Larger chunks reduce the number of chunk files and random file opens, which can help Windows storage paths. The tradeoff is that each ready chunk is heavier to upload and draw, so keep `CAVEVIEWER_UPLOAD_CHUNKS_PER_FRAME=1` when testing `32m` or `48m` chunks.

The splash screen's **Advanced Settings** dialog is split into **Streaming Performance** and **Map Parsing** sections. Streaming settings affect runtime loading and GPU upload pacing. Map Parsing settings affect new imports or rebuilt caches; they do not reinterpret an existing `.caveviewer_cache`.

Cache-build tuning examples (import/chunk generation phase):

```bash
# macOS / Linux: constrain cache-build worker count
export CAVEVIEWER_CHUNK_BUILD_WORKERS=4
./run_caveviewer.sh
```

```powershell
# Windows PowerShell: constrain cache-build worker count
$env:CAVEVIEWER_CHUNK_BUILD_WORKERS = "4"
python caveviewer.py
```

- `CAVEVIEWER_UI_TEXT_SCALE`
  Global UI text scale override (float).

- `CAVEVIEWER_UI_FONT`
  Absolute path to a `.ttf/.otf/.ttc` font file for UI text rendering.

- `CAVEVIEWER_TEXT_AA_MODE`
  UI text anti-aliasing mode: `normal`, `light`, or `lcd`.

- `CAVEVIEWER_FORCE_STARTUP_FOCUS`
  Startup focus override for frozen macOS builds (`1/true/yes/on` to enable).

### Update configuration

- `CAVEVIEWER_UPDATE_MANIFEST_URL`
  Explicit update manifest URL (highest priority).

- `CAVEVIEWER_GITHUB_REPO`
  GitHub repo used to derive default update manifest URLs when explicit URL is not set.

### Windows-specific update overrides

- `CAVEVIEWER_WINDOWS_GITHUB_REPO`
  Windows adapter repo override for update metadata.

- `CAVEVIEWER_WINDOWS_UPDATE_MANIFEST_URL`
  Windows-specific update manifest URL override.

### Development and packaging

- `CAVEVIEWER_DEV_VENV`
  Development virtual environment path used by `scripts/dev/install.sh` and `run_caveviewer.sh`.

- `CAVEVIEWER_LINUX_BUILD_VENV`
  Linux packaging virtual environment path.

- `CAVEVIEWER_MACOS_BUILD_VENV`
  macOS packaging virtual environment path.

### Large-map tuning

For very large source maps (for example `.obj` files around 10-20GB+), you can increase cache chunk size to reduce the number of chunk files and improve streaming performance on some systems (especially Windows random-I/O workloads).

- Environment variable: `CAVEVIEWER_CHUNK_SIZE_METERS`
- Default: `8.0` (current behavior)
- Recommended starting values for very large maps: `16` or `24`

Examples:

```bash
# macOS / Linux
export CAVEVIEWER_CHUNK_SIZE_METERS=16
./run_caveviewer.sh
```

```powershell
# Windows PowerShell
$env:CAVEVIEWER_CHUNK_SIZE_METERS = "16"
python caveviewer.py
```

Use larger chunk sizes only when needed: very large values can increase per-chunk draw cost, while smaller values increase chunk-file count.

## Troubleshooting

If you encounter issues, check the console output for error messages and ensure all required files are present in the selected folder.
