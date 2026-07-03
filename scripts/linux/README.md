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
wget https://github.com/AppImage/AppImageKit/releases/download/13/appimaketool-x86_64.AppImage

# Make it executable
chmod +x appimagetool-x86_64.AppImage

# Install to system PATH
sudo mv appimagetool-x86_64.AppImage /usr/local/bin/appimaketool
```

### System Libraries
The build will automatically bundle most libraries, but ensure you have development headers:

```bash
# Ubuntu/Debian
sudo apt-get install libfreetype6-dev libgl1-mesa-dev

# Fedora/RHEL
sudo dnf install freetype-devel mesa-libGL-devel

# Arch
sudo pacman -S freetype mesa
```

## Build Steps

### Step 1: Build PyInstaller Bundle
Creates a self-contained Python environment with all dependencies:

```bash
./scripts/linux/build_linux_app.sh
```

**Output:** `dist/linux/app/CaveViewer/` - a complete self-contained application directory

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
./scripts/linux/package.sh
```

**Output:** `dist/linux/packages/CaveViewer-VERSION-x86_64.AppImage` - ready to distribute

**What it does:**
- Creates AppDir structure (standard AppImage format)
- Adds `.desktop` file for app menu integration
- Includes app icon
- Creates `AppRun` wrapper script
- Runs `appimaketool` to create final `.AppImage` executable

### Step 3: Full Release Workflow
One-command build + package + manifest generation:

```bash
./scripts/linux/publish_release.sh
```

**Output:**
- AppImage executable (dist/linux/packages/)
- Release manifest with SHA256 and size info
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
./scripts/linux/build_linux_app.sh
./scripts/linux/package.sh

# Upload to GitHub release:
gh release upload v1.2.45 dist/linux/packages/*.AppImage
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
│       ├── CaveViewer            # Symlink to main executable
│       ├── usr/share/
│       │   ├── applications/
│       │   │   └── caveviewer.desktop
│       │   └── icons/
│       │       └── hicolor/256x256/apps/caveviewer.png
│       └── lib/                  # Libraries (copied from bundle)
└── packages/
    └── CaveViewer-1.2.45-x86_64.AppImage  # Final distributable
```

## Troubleshooting

### "appimaketool not found"
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
ls -la dist/linux/app/CaveViewer/

# 5. Test before AppImage packaging
./dist/linux/app/CaveViewer/CaveViewer
```

### Custom AppImage Creation
If you need finer control, you can use `linuxdeploy` for better library bundling:
```bash
# Install linuxdeploy
wget https://github.com/linuxdeploy/linuxdeploy/releases/download/continuous/linuxdeploy-x86_64.AppImage
chmod +x linuxdeploy-x86_64.AppImage

# Use in package.sh instead of direct appimaketool
./linuxdeploy-x86_64.AppImage --appdir=$appdir --deploy-deps-only --output=appimage
```

## See Also
- [AppImage Documentation](https://docs.appimage.org/)
- [PyInstaller Manual](https://pyinstaller.org/en/stable/)
- [Linux update manifest](../updates/linux/stable.json)
