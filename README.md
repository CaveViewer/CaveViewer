# CaveViewer

A standalone viewer for large cave-survey 3D mesh maps, built for maps too big to comfortably load all at once. Instead of loading the whole mesh into memory/VRAM, CaveViewer splits it into a 3D grid of spatial chunks and only keeps the chunks near your current position loaded -- so frame rate stays smooth no matter how big the full cave system is.

Supports two source formats:

- **OBJ** (+ matching `.mtl` + tiled `.jpg` textures) -- the original format this was built for, exported from Agisoft Metashape.

- **GLB** (binary glTF) -- including maps whose textures are embedded directly inside the file rather than as separate images.

Whichever format a map is in, the rest of the program (chunking, streaming, all the on-screen controls) behaves identically -- format only matters at the moment a map is first opened.

## How it works

1. **First time opening a map:** CaveViewer parses your `.obj` (streaming, so it doesn't choke on multi-GB files) and splits it into a grid of spatial chunks (default 8m cubes), writing a cache folder `.caveviewer_cache` next to your `.obj` file. This is a one-time cost -- for a 10-20 million triangle map, expect roughly 30-90 seconds depending on your CPU and disk speed.

2. **Every time after:** CaveViewer detects the existing cache and skips straight to launching the viewer -- near-instant.

3. **While flying around:** only the chunks within a few grid cells of your camera are loaded into GPU memory. As you fly deeper into the cave, far chunks behind you are unloaded and new chunks ahead of you stream in, automatically, in a background thread so it doesn't stall your frame rate.

## How to install and run CaveViewer

### macOS app (recommended)

Download the latest DMG from https://github.com/KernalPanic/CaveViewerPlus/releases, open it, and drag CaveViewer into Applications. If this is your first time installing the app, you have to go to Settings -> Privacy & Security and then allow system to open CaveViewerPlus. These steps are necessary the app is not published through the App Store.

If there is an update, the app will let you download and install it.

### Windows app (recommended)

Download the latest zip from https://github.com/KernalPanic/CaveViewerPlus/releases and extract it anywhere on your machine.

**First-time setup:** double-click `launch.bat` inside the extracted folder. This opens a guided setup window that will:

1. Install Python 3.12 if it is not already present (downloads from python.org and registers it on your PATH automatically — a UAC prompt will appear, which is expected).
2. Install the required Python packages from `requirements.txt`.
3. Create a Desktop shortcut so you can launch CaveViewer with a double-click going forward.

> **Note:** `launch.bat` exists because Windows blocks `.ps1` files from running when double-clicked. The bat file launches `scripts/windows/setup.ps1` with the correct flags — it does not change any permanent system security settings.

After setup completes the window closes automatically. Use the Desktop shortcut (or re-run `launch.bat`) to start CaveViewer.

If there is an update, the app will let you download and install it.

The update manifests are platform-specific, so the macOS app reads `updates/macos/stable.json` and the Windows app reads `updates/windows/stable.json`.

### Linux app (AppImage)

The best-practice distribution format for Linux is the self-contained AppImage — a single executable file that bundles Python, all dependencies, and the app itself. No system-wide installation or package manager involvement required.

**1. Download the AppImage**

Download `CaveViewer-<version>-x86_64.AppImage` from https://github.com/KernalPanic/CaveViewerPlus/releases.

**2. Make it executable and run**

```bash
chmod +x CaveViewer-*-x86_64.AppImage
./CaveViewer-*-x86_64.AppImage
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

**2. Create a virtual environment and install dependencies**

`scripts/dev/install.sh` creates a `.venv` inside the project root, installs all packages from `requirements.txt`, and generates a `run_caveviewer.sh` launcher:

```bash
./scripts/dev/install.sh
```

**3. (Optional) Configure environment variables**

`scripts/dev/env_setup.sh` sets `PYTHONPATH`, `CAVEVIEWER_HOME`, and `CAVEVIEWER_GITHUB_REPO` for the current shell session. Source it (don't execute it) so the variables are exported to your shell:

```bash
source ./scripts/dev/env_setup.sh
```

You can also override the update manifest URL by uncommenting and setting `CAVEVIEWER_UPDATE_MANIFEST_URL` inside that file.

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

### macOS

```bash
# Build the DMG
./scripts/release.sh macos-package

# Publish to GitHub and update the macOS update manifest
./scripts/release.sh macos-publish <version> "Release notes"

# Example
./scripts/release.sh macos-publish 1.2.3 "Bug fixes and stability improvements"
```

### Windows

```bash
# Build the zip
./scripts/release.sh windows-package

# Publish to GitHub and update the Windows update manifest
./scripts/release.sh windows-publish <version> "Release notes"

# Example
./scripts/release.sh windows-publish 1.2.3 "Bug fixes and stability improvements"
```

### Linux

Linux builds must be created on a Linux host (or inside the provided Docker container — see `scripts/linux/README.md`).

```bash
# Build the AppImage (must run on Linux)
./scripts/release.sh linux-package

# Publish to GitHub and update the Linux update manifest
./scripts/release.sh linux-publish <version> "Release notes"

# Example
./scripts/release.sh linux-publish 1.2.3 "Bug fixes and stability improvements"
```

The publish scripts build the platform artifact, create or update the GitHub release tag (`v<version>`), and write the corresponding update manifest (`updates/macos/stable.json` or `updates/windows/stable.json`) so the in-app updater picks up the new version.

## Minimap

A small panel in the bottom-left corner shows a top-down outline of the entire cave system's footprint, with a red dot marking your current position.

## Troubleshooting

If you encounter issues, check the console output for error messages and ensure all required files are present in the selected folder.