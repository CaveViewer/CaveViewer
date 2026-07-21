# CaveViewer

CaveViewer makes exploring massive 3-D maps accessible to everyone. Designed for cave divers, explorers, cartographers, and 3-D mapping enthusiasts, it uses an innovative rendering technique to display extremely large maps on anything from lightweight laptops to high-end workstations. Released as open-source software under the GNU GPL v3 license, CaveViewer is completely free—no advertisements, no subscriptions, no online accounts, and no hidden costs.

CaveViewer supports several common 3-D map formats, including OBJ models exported from Agisoft Metashape, GLB files, and previously optimized CaveViewer cache files.

No matter which format you open, the experience is the same. CaveViewer automatically prepares the map for viewing, allowing you to explore even extremely large 3-D maps with the same navigation, rendering, and tools regardless of the original file format.

## How to Install and Run CaveViewer

## Supported Platforms

CaveViewer is currently available for the following platforms:

- macOS (Apple Silicon and Intel)
- Linux (Fedora and Ubuntu)
- Windows (10 and 11)

Support for additional operating systems and distributions may be added in the future based on community interest and user demand. If your preferred platform is not currently supported, we'd love to hear from you.

### macOS

Download the latest DMG from https://github.com/CaveViewer/CaveViewer/releases.
Use `CaveViewer-<version>-macos-arm64.dmg` on Apple Silicon or
`CaveViewer-<version>-macos-x86_64.dmg` on an Intel Mac. Open it and drag
CaveViewer into Applications.

If this is your first time installing the app, you have to go to Settings -> Privacy & Security and then allow the system to open CaveViewer. These steps are necessary because the app is not published through the App Store.

### Windows

Download the latest zip from https://github.com/CaveViewer/CaveViewer/releases and extract it anywhere on your machine. Then click on `launch.bat` inside the folder to install.

### Linux

The best-practice distribution format for Linux is the self-contained AppImage — a single executable file that bundles Python, all dependencies, and the app itself. No system-wide installation or package manager involvement required.

Download the Linux x86_64 AppImage from https://github.com/CaveViewer/CaveViewer/releases:

- `CaveViewer-<version>-x86_64.AppImage` (amd64 / x86_64)

Then change the permissions to make the file executable and run.

```bash
chmod +x CaveViewer-*.AppImage

./CaveViewer-*-x86_64.AppImage   # amd64 / x86_64
```

On GNOME, Linux builds use GLFW with `CAVEVIEWER_WINDOW_SYSTEM=auto` by
default. Auto mode prefers X11/XWayland when `DISPLAY` is available so source,
debugger, and AppImage launches get the same titlebar and resize behavior, then
retries Wayland on recognized initialization failures. Directory choosers and
update reveal actions use the desktop portal. To diagnose a
compositor-specific issue, launch with `CAVEVIEWER_WINDOW_SYSTEM=wayland` or
`CAVEVIEWER_WINDOW_SYSTEM=x11` to require one backend. The viewer opens at 80%
of the primary monitor's usable work area using the selected backend's scaled
screen coordinates.

### In-app update downloads

The splash screen checks the signed update manifest for the current platform
and architecture. If you start a download, it continues in the background
while you open maps, download sample maps, or change settings. When the splash
screen is visible it owns update progress and completion feedback; if a
download completes in the background, CaveViewer may use a desktop
notification instead of interrupting map viewing.

After verification, CaveViewer keeps the package in `~/Downloads` and reveals
it for manual installation: macOS mounts the DMG read-only and shows the app in
Finder, Windows selects the package in Explorer, and Linux opens the download
folder. CaveViewer never executes the package or installs the update. Closing
the whole application cancels an unfinished download and removes its temporary
files; a verified package remains in `~/Downloads`.

## Getting Started with Sample Maps

If you want to try CaveViewer without your own scan, use the built-in sample maps.

1. Start CaveViewer and open the splash screen.
2. Click `Download sample maps` below the main map-open button.
3. Pick one of the sample maps in the dialog.
4. If it is not already downloaded, choose a folder to save it. The dialog stays responsive while CaveViewer downloads and extracts the map in the background, and the row button can cancel the active download.
5. When the button changes to `Open`, click it to load the sample map.

Sample maps are a good way to confirm that CaveViewer is working before you import your own data. You can also reopen them later from the same sample maps dialog.

## Recording a Flight

CaveViewer can record a clean flight through the cave. Recordings are currently
encoded as MP4 files with `ffmpeg`.

Use the `REC` button to arm recording. The minimap, controls, and control panel
disappear immediately, a 3-to-0 countdown appears in the amber loading ring,
and then recording begins. Press `Shift+R` to cancel the countdown or stop
recording.

Videos are saved to:

```text
~/Movies/CaveViewer/
```

Frames are streamed directly to the video encoder while you fly. CaveViewer does
not keep the recording in memory. Recordings are scaled to a 1080-pixel maximum
height by default so they play back smoothly on normal video players.


## Importing and Streaming Preferences

The Preferences panel in the startup window controls import and streaming behavior. Open it from the splash screen with Preferences.

These values are validated in the UI, applied to environment variables for the current launch, and saved to a local preferences file so they are reused next time.

- Linux preferences file: `~/.config/caveviewer/advanced_settings.json` by default
- macOS/Windows preferences file: `~/.caveviewer/advanced_settings.json`

The stored preferences file keeps the legacy-compatible `advanced_settings.json`
filename; the UI and source APIs use Preferences terminology.

- Streaming section controls runtime chunk loading and upload behavior.
- Import section controls cache-build/import behavior.
- Storage section controls folders used when saving recordings and other app data.

### Rendering, Import, and Streaming

CaveViewer imports large maps into managed chunk caches, then streams only the
nearby working set while you move. Import settings change future cache builds;
streaming settings tune runtime loading and GPU upload behavior without
re-importing the map.

For the full rendering philosophy, low-memory recommendations, runtime
environment variables, and `caveviewer-chunker` CLI options, see
[`README-rendering.md`](README-rendering.md).

### Recordings

| Preference | Environment variable | Default | Valid range | What it changes |
|---|---|---:|---|---|
| Recordings folder | `CAVEVIEWER_RECORDING_DIR` | `~/Movies/CaveViewer` | writable folder, or a folder that can be created | Where saved recordings are stored. |

## Contributing

See [`CONTRIBUTING.md`](CONTRIBUTING.md) for the change workflow and
[`docs/development/`](docs/development/README.md) for architecture, repository
layout, coding, testing, and AI-assisted development standards.

## License

CaveViewer is free software licensed under the GNU General Public License version 3.0. See `LICENSE` for the full license text.

Third-party dependency notices are listed in `THIRD_PARTY_NOTICES.md`.
