# CaveViewer

CaveViewer makes exploring massive 3-D maps accessible to everyone. Designed for cave divers, explorers, cartographers, and 3-D mapping enthusiasts, it uses an innovative rendering technique to display extremely large maps on anything from lightweight laptops to high-end workstations. Released as open-source software under the GNU AGPL v3 license, CaveViewer is completely free—no advertisements, no subscriptions, no online accounts, and no hidden costs.

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

Download `CaveViewer-<version>-windows.exe` from the latest
[CaveViewer release](https://github.com/CaveViewer/CaveViewer/releases), then
run it. The signed CaveViewer Setup installer installs the application for the
current user and creates its shortcuts; no Python installation, virtual
environment, archive extraction, or batch file is required. Existing ZIP
releases remain only for users completing the migration from an older release.

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

### In-app updates

The splash screen checks the signed update manifest for the current platform
and architecture. If you start a download, it continues in the background while
you open maps, download standard library maps, or change settings. When the
splash screen is visible it owns update progress and completion feedback; if a
download completes in the background, CaveViewer may use a desktop notification
instead of interrupting map viewing.

After verification, macOS and Linux keep the package in `~/Downloads` and
reveal it for manual installation. Windows ZIP migration packages are handled
the same way. A CaveViewer installed by the signed Windows EXE instead shows an
explicit `Install and restart <version>` action for a signed EXE update. After
that click, CaveViewer keeps the EXE under the current user's
`%LOCALAPPDATA%\CaveViewer\updates`, rechecks its size, SHA-256, Authenticode
publisher, chain, and timestamp, then starts the installer update contract and
exits normally. The installer waits for CaveViewer, verifies the new frozen
payload, and relaunches it. A source/ZIP installation never auto-executes an
EXE; it receives the manual migration path instead.

## Getting Started with the Map Library

If you want to try CaveViewer without your own scan, use the built-in map
library.

1. Start CaveViewer and open the splash screen.
2. Use the `Map Library` panel to reopen a recent map or pick an available
   standard library map.
3. Click `Get` for a standard library map that is not downloaded yet. The
   panel stays responsive while CaveViewer downloads and extracts the map in
   the background, and the row button can cancel the active download.
4. When a map is already downloaded, click `Open` to load it.

The map library is a good way to confirm that CaveViewer is working before you
import your own data. By default, CaveViewer stores downloaded Map Library maps
in `~/Downloads`; the Storage section in Preferences controls that folder, and
older app-data `map_library` or `sample_maps` directories are moved there
automatically when possible. The CaveViewer Maps list is refreshed from
CaveViewer's GitHub-hosted map catalog when the splash screen is online and
falls back to the last cached or bundled catalog when it is offline. Generated
caches for downloaded maps live in each map's `_cache` subdirectory.
Use `Remove map files` to remove the downloaded map folder and its cache,
or `Remove cache` to remove only the generated `_cache` folder.

For an eligible recent or downloaded map, choose `Rebuild cache` from its
overflow menu to recreate the cache with the current Import preferences without
opening the map. The old cache remains available until its replacement is
ready. OBJ rebuilds can be paused, and CaveViewer may show a desktop
notification for completion or failure while the splash screen is not focused.

## Capture controls

Video recording (`Ctrl/Cmd+R`), manual dive tracing (`Ctrl/Cmd+T`), and cave
slicing (`Ctrl/Cmd+C`) are mutually exclusive: finish or cancel the current
capture before starting another. While one capture owns the viewer, shortcuts
for the other capture types are silently ignored; the owner's shortcut remains
the active finish-and-save control. Each countdown shows that shortcut together
with **Press Esc to cancel**. Video recording, dive tracing, and cave slicing
all leave the cave view clear after their countdown.
The normal shortcut preserves the completed MP4, JSONL trace, or cave slice;
Escape discards the capture, releases its queued buffers and background work,
and removes partial output. After cleanup, a three-second confirmation names
the canceled capture and states that it was not saved; the viewer closes after
that confirmation has remained readable for the full pause. A slice remains
cancelable with Escape while its background export is cleaning up. Closing the
viewer with its native window control instead preserves an active artifact and
uses the separate **Finishing…** save-on-close status.

## Recording a Flight

CaveViewer can record a clean flight through the cave. Recordings are currently
encoded as MP4 files with `ffmpeg`.

Use `Ctrl+R` (`Cmd+R` on macOS) to arm recording. The
minimap, controls, and control panel disappear immediately, a 3-to-0 countdown
appears in the amber loading ring, and then recording begins. Press the same
shortcut again to stop and save, or press `Escape` to cancel and remove the
partial recording before the viewer closes.

Videos are saved to:

```text
~/Movies/CaveViewer/
```

Frames are streamed directly to the video encoder while you fly. CaveViewer does
not keep the recording in memory. Recordings are scaled to a 720-pixel maximum
height by default to keep render readback and encoding costs lower. Set
`CAVEVIEWER_RECORDING_MAX_HEIGHT=1080` to opt back into 1080p recording.


## Saving a Cave Slice

While viewing a precompiled cave map, press `Ctrl+C` (`Cmd+C` on macOS) to
start a 3-2-1 slice countdown. Once it completes, fly through the passage you
want to keep and press the shortcut again to finish and save the slice.
Press `Escape` to cancel the countdown, active selection, or background export.
CaveViewer selects the render chunks intersecting that region and copies each
complete chunk into a new standalone map without rewriting its triangles. This
preserves the original walls, materials, texture mapping, normals, and detailed
minimap footprint. Because chunks are kept intact, the saved geometry can
extend to a source chunk boundary beyond the two camera endpoints. CaveViewer
shows save progress, then opens the slice folder in the system file explorer.
There is no slice toolbar button.

The new map is stored under the Preferences **Downloaded maps folder** location
using the cave's name and a segment number, such as `Ginnie Springs - Segment
1`. Later slices from that cave receive the next segment number. It includes
its own lossless chunks, manifest, minimap footprint, and needed textures, so
copying that whole folder to another computer is enough to view it there
without the original map. Closing the viewer after slicing has started saves
using the camera's final position as the slice endpoint and closes only after
the export finishes.


## Importing and Streaming Preferences

The Preferences panel in the startup window controls import and streaming behavior. Open it from the splash screen with Preferences.

These values are validated in the UI, applied to environment variables for the current launch, and saved to a local preferences file so they are reused next time.

- Linux preferences file: `~/.config/caveviewer/preferences.json` by default
- macOS/Windows preferences file: `~/.caveviewer/preferences.json`

When `preferences.json` does not yet exist, CaveViewer renames a sibling
`advanced_settings.json` once so existing preferences remain available. Older
preference filenames are not discovered.

- Streaming section controls runtime chunk loading and upload behavior.
- Import section controls cache-build/import behavior.
- Storage section controls folders used when saving recordings and downloaded
  map-library entries.
- Backup saves a complete `preferences.json`, loads a shared
  file for review, or stages the built-in defaults. Imported and restored
  values are not saved until you select **Save changes**; **Discard changes**
  restores the previously saved preferences.
- Leaving Preferences with pending edits offers **Save changes**, **Discard
  changes**, and **Keep editing** so navigation cannot silently lose changes.

Exports open a native Save dialog in a user-visible location. Imports use the
native Open dialog and accept UTF-8 JSON objects up to 256 KiB. Missing or
invalid individual values use the current defaults without discarding other
valid values; unknown keys are ignored. An unreadable, malformed, or non-object
file is rejected without changing the form or saved preferences.
Recording and map-library folders remain unchanged when loading a backup
because those locations are specific to the destination installation.

The first launch that finds `advanced_settings.json` beside a missing
`preferences.json` renames it automatically. If both exist, `preferences.json`
wins and the older file is left untouched. The still older
`.caveviewer_advanced_settings.json` filename is not supported.

### Rendering, Import, and Streaming

CaveViewer imports large maps into generated chunk caches, stored in `_cache`
inside the map folder by default, then streams only the nearby working set while
you move. Import settings change future cache builds; streaming settings tune
runtime loading and GPU upload behavior without re-importing the map.

For the full rendering philosophy, low-memory recommendations, runtime
environment variables, and `caveviewer-chunker` CLI options, see
[`docs/development/rendering.md`](docs/development/rendering.md).

### Storage

| Preference | Environment variable | Default | Valid range | What it changes |
|---|---|---:|---|---|
| Recordings folder | `CAVEVIEWER_RECORDING_DIR` | `~/Movies/CaveViewer` | writable folder, or a folder that can be created | Where saved recordings are stored. |
| Downloaded maps folder | `CAVEVIEWER_MAP_LIBRARY_DIR` | `~/Downloads` | writable folder, or a folder that can be created | Where downloaded Map Library maps are stored. |

## Troubleshooting and Logs

Open **Help > Troubleshooting** from CaveViewer's startup window when you need
diagnostic information. **Show latest log** opens the application log folder
and selects the newest session log in Explorer, Finder, or the Linux file
browser. The same tab shows the last recorded error with its three preceding
log lines; use **Copy** beside **Last error** to copy that excerpt for a support
request.

If the current session has not written a log or error yet, the tab shows an
empty-state explanation instead of opening an unrelated file. At startup,
CaveViewer removes session logs older than 24 hours and also keeps no more than
the newest ten sessions. The Help action does not expose structured JSONL
diagnostics.

## Contributing

See [`CONTRIBUTING.md`](CONTRIBUTING.md) for the change workflow and
[`docs/development/`](docs/development/README.md) for architecture, repository
layout, coding, testing, and AI-assisted development standards.

## License

CaveViewer is free software licensed under the GNU Affero General Public License version 3.0 only. See `LICENSE` for the full license text.

Third-party dependency notices are listed in `THIRD_PARTY_NOTICES.md`.
