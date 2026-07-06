# Linux AppImage Build Guide

This guide explains how to build a self-contained AppImage for CaveViewer on Linux.

## Prerequisites

### System Requirements
- Linux (Ubuntu, Fedora, Arch, Debian, etc.)
- Python 3.10+
- `appimagetool` (AppImage creation tool)

### Install appimagetool

```bash
# Download the latest appimagetool
wget https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-x86_64.AppImage

# Make it executable
chmod +x appimagetool-x86_64.AppImage

# Install to system PATH
sudo mv appimagetool-x86_64.AppImage /usr/local/bin/appimagetool
```

### System Libraries
The build will automatically bundle most libraries, but ensure you have development headers:

```bash
# Ubuntu/Debian
sudo apt-get install file fontconfig fonts-noto-core libfreetype6-dev libgl1-mesa-dev

# Fedora/RHEL
sudo dnf install file fontconfig google-noto-sans-fonts freetype-devel mesa-libGL-devel

# Arch
sudo pacman -S file fontconfig noto-fonts freetype mesa
```

## Build Steps

### Step 1: Build PyInstaller Bundle
Creates a self-contained Python environment with all dependencies:

```bash
./scripts/linux/common/build.sh
```

**Output:** `dist/linux/<arch>/app/CaveViewer/` - a complete self-contained application directory

**What it does:**
- Creates/uses an architecture-specific Python virtual environment by default:
    - `.venv-linux-build-arm64` on arm64
    - `.venv-linux-build-amd64` on amd64
- You can override with `CAVEVIEWER_LINUX_BUILD_VENV=/path/to/venv`
- Installs all dependencies from `requirements.txt`
- Runs PyInstaller to bundle Python + all packages + assets
- Uses `--onedir` mode (easier for AppImage integration)

### Step 2: Package as AppImage
Wraps the PyInstaller output into a distributable AppImage:

```bash
./scripts/linux/common/package.sh
```

**Output:** `dist/linux/<arch>/packages/CaveViewer-VERSION-ARCH.AppImage` - ready to distribute

**What it does:**
- Creates AppDir structure (standard AppImage format)
- Adds `.desktop` file for app menu integration
- Includes app icons in standard hicolor sizes for GNOME/Fedora/Ubuntu lookup
- Creates `AppRun` wrapper script
- Runs `appimagetool` to create final `.AppImage` executable
- Validates that `appimagetool` both matches the CPU architecture and can
  execute in the current build container. If the cached/system tool is missing,
  wrong, or unusable, the script downloads the matching `x86_64` or `aarch64`
  tool into `dist/linux/tools/`.

### Step 3: Full Release Workflow
Each Linux architecture is published as its own platform target:

```bash
./scripts/linux/arm64/publish.sh
# or
./scripts/linux/x86_64/publish.sh
```

**Output:**
- AppImage executable (`dist/linux/<arch>/packages/`)
- Architecture-specific release manifest with SHA256 and size info
- Ready for GitHub release upload

## Usage

### For End Users
```bash
# Download from GitHub release
wget https://github.com/.../CaveViewer-1.2.45-x86_64.AppImage

# Make executable
chmod +x CaveViewer-1.2.45-x86_64.AppImage

# Run
./CaveViewer-1.2.45-x86_64.AppImage
```

### For CI/CD Integration
```bash
# In your GitHub Actions workflow (must run on a Linux runner):
./scripts/linux/common/build.sh
./scripts/linux/common/package.sh

# Upload to GitHub release:
gh release upload v1.2.45 dist/linux/x86_64/packages/*.AppImage
```

## Advantages of This Approach

✓ **Fully self-contained** - Works on any Linux distro without system dependency hell
✓ **Single executable** - Just download and run, no installation needed
✓ **Auto-updates** - Integrates with existing CaveViewer update checker
✓ **Desktop integration** - Appears in app menus and launchers
✓ **Sandboxed** - Doesn't interfere with system Python or packages

## File Structure

```
dist/linux/
├── app/
│   ├── CaveViewer/               # PyInstaller bundle (intermediate)
│   │   ├── CaveViewer            # Main executable
│   │   ├── lib/                  # Bundled libraries
│   │   ├── libPython.so.x.y      # Bundled Python runtime
│   │   └── ...
│   └── CaveViewer.AppDir/        # AppImage staging directory
│       ├── AppRun                # Entry point script
│       ├── CaveViewer            # Symlink to bundled executable
│       ├── caveviewer.desktop
│       ├── caveviewer.png
│       ├── usr/lib/caveviewer/   # Copied PyInstaller bundle
│       ├── usr/share/
│       │   ├── applications/
│       │   │   └── caveviewer.desktop
│       │   └── icons/
│       │       └── hicolor/
│       │           ├── 48x48/apps/caveviewer.png
│       │           ├── 64x64/apps/caveviewer.png
│       │           ├── 128x128/apps/caveviewer.png
│       │           ├── 256x256/apps/caveviewer.png
│       │           └── 512x512/apps/caveviewer.png
│       └── lib/                  # Libraries (copied from bundle)
└── packages/
    └── CaveViewer-1.2.45-x86_64.AppImage  # Final distributable
```

## Troubleshooting

### "appimagetool not found"
Install it as described in Prerequisites section above.

### Build fails on dependency
Make sure `requirements.txt` is up to date and compatible with Python 3.10+:
```bash
pip install -r requirements.txt
```

### AppImage won't start
Try running with verbose output to debug:
```bash
./CaveViewer-1.2.45-x86_64.AppImage --verbose
```

### OpenGL errors at runtime
The bundled libGL might conflict with system libraries. Workaround:
```bash
QT_QPA_PLATFORM_PLUGIN_PATH="" ./CaveViewer-1.2.45-x86_64.AppImage
```

## For Developers

### Manual Build Steps (debugging)
```bash
# 1. Create venv
python3 -m venv .venv-linux-build-amd64
source .venv-linux-build-amd64/bin/activate

# 2. Install deps
pip install -r requirements.txt
pip install pyinstaller==6.21.0

# 3. Build with PyInstaller
python -m PyInstaller --onedir CaveViewer.spec

# 4. Inspect bundle
ls -la dist/linux/x86_64/app/CaveViewer/

# 5. Test before AppImage packaging
./dist/linux/x86_64/app/CaveViewer/CaveViewer
```

### Custom AppImage Creation
If you need finer control, you can use `linuxdeploy` for better library bundling:
```bash
# Install linuxdeploy
wget https://github.com/linuxdeploy/linuxdeploy/releases/download/continuous/linuxdeploy-x86_64.AppImage
chmod +x linuxdeploy-x86_64.AppImage

# Use in package.sh instead of direct appimagetool
./linuxdeploy-x86_64.AppImage --appdir=$appdir --deploy-deps-only --output=appimage
```

## See Also
- [AppImage Documentation](https://docs.appimage.org/)
- [PyInstaller Manual](https://pyinstaller.org/en/stable/)
- Linux update manifests:
  - `updates/linux/arm64/stable.json`
  - `updates/linux/x86_64/stable.json`
