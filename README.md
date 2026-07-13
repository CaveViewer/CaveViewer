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

Download the latest DMG from https://github.com/KernalPanic/CaveViewer/releases.
Use `CaveViewer-<version>-macos-arm64.dmg` on Apple Silicon or
`CaveViewer-<version>-macos-x86_64.dmg` on an Intel Mac. Open it and drag
CaveViewer into Applications.

If this is your first time installing the app, you have to go to Settings -> Privacy & Security and then allow the system to open CaveViewer. These steps are necessary because the app is not published through the App Store.

### Windows

Download the latest zip from https://github.com/KernalPanic/CaveViewer/releases and extract it anywhere on your machine. Then click on `launch.bat` inside the folder to install.

### Linux

The best-practice distribution format for Linux is the self-contained AppImage — a single executable file that bundles Python, all dependencies, and the app itself. No system-wide installation or package manager involvement required.

Download the Linux x86_64 AppImage from https://github.com/KernalPanic/CaveViewer/releases:

- `CaveViewer-<version>-x86_64.AppImage` (amd64 / x86_64)

Then change the permissions to make the file executable and run.

```bash
chmod +x CaveViewer-*.AppImage

./CaveViewer-*-x86_64.AppImage   # amd64 / x86_64
```

On GNOME, Linux builds use the native Wayland GLFW backend by default and fall
back to X11 when automatic initialization fails. Directory choosers and update
reveal actions use the desktop portal. To diagnose a compositor-specific issue,
launch with `CAVEVIEWER_WINDOW_SYSTEM=wayland` or
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

### Virtual Machines or GPU Driver Problems

If CaveViewer crashes, freezes, or leaves a stuck process when running inside a
virtual machine such as Parallels, start it with virtual sync disabled:

```bash
CAVEVIEWER_VSYNC=0 ./CaveViewer-*.AppImage
```

If the problem still happens, force software OpenGL rendering as a stronger
fallback:

```bash
LIBGL_ALWAYS_SOFTWARE=1 CAVEVIEWER_VSYNC=0 ./CaveViewer-*.AppImage
```

`LIBGL_ALWAYS_SOFTWARE=1` tells the Linux OpenGL stack to use software rendering
instead of the GPU driver. This can be slower, but it can avoid crashes or
kernel-level hangs caused by virtual GPU drivers or unstable graphics drivers.

## Getting Started with Sample Maps

If you want to try CaveViewer without your own scan, use the built-in sample maps.

1. Start CaveViewer and open the splash screen.
2. Click `Download sample maps` below the main map-open button.
3. Pick one of the sample maps in the dialog.
4. If it is not already downloaded, choose a folder to save it and wait for the download to finish.
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

The Preferences panel in the startup window acts as the advanced installer/preferences surface for import and streaming behavior. Open it from the splash screen with Preferences.

These values are validated in the UI, applied to environment variables for the current launch, and saved to a local settings file so they are reused next time.

- Linux settings file: `~/.config/caveviewer/advanced_settings.json` by default
- macOS/Windows settings file: `~/.caveviewer/advanced_settings.json`
- Streaming section controls runtime chunk loading and upload behavior.
- Import section controls cache-build/import behavior.
- Storage section controls folders used when saving recordings and other app data.

### Map Chunking

When you import a map, CaveViewer does not keep it as one giant object. It splits the map into many smaller 3D chunks and saves them in a cache. During viewing, CaveViewer loads only the chunks near you and unloads chunks farther away.

Why this matters: chunk size is one of the most important map settings because it strongly affects smoothness, pop-in, memory usage, and how far ahead the map can appear to load cleanly while you move.

- Larger chunks: fewer load/unload events, often smoother in long/open passages, but can increase per-chunk cost.
- Smaller chunks: finer-grained loading and culling, often useful in tight/twisty areas, but can increase chunk churn.


### Streaming Performance

| Preference | Environment variable | Default | Valid range | What it changes |
|---|---|---:|---|---|
| System RAM target (%) | — | 8 | 1 to 80 | Target share of total system RAM used for loaded chunks. |
| GPU memory target (%) | — | 70 | 1 to 80 | Target share of detected GPU memory used for loaded chunks. |
| GPU memory override (GB) | — | empty | 0.5 to 50 | Optional manual GPU memory ceiling. If auto-detection finds a smaller active GPU, CaveViewer uses the smaller detected value. |
| Loading worker limit | — | 2 | integer, 1 to 32 | Background chunk-loading worker limit. Worker count may be lower when CPU or RAM is constrained. |
| Loading CPUs to keep free | — | 3 | integer, 2 to 32 | Reserve CPU cores instead of using them for streaming workers. |
| Chunk uploads per frame | — | 1 | integer, 1 to 16 | Hard cap for how many ready chunks are uploaded each frame on the render thread. |
| Upload budget (ms) | — | 3.0 | 0.5 to 50.0 ms | Soft time budget per frame for chunk uploads. |

GPU memory is detected automatically through Linux DRM sysfs for AMD GPUs and
through `nvidia-smi` for NVIDIA GPUs. Use the GPU memory override only when
automatic detection is unavailable or does not match the active adapter. If
GPU memory cannot be detected and no override is set, CaveViewer applies a
conservative 1 GB fallback instead of disabling the GPU residency cap.
CaveViewer keeps geometry visibility separate from texture residency: if a map
has too many very large texture tiles for the GPU budget, oversized textures are
downscaled during decode instead of dropping nearby chunks from the visible
world. For debugging or benchmarking, `CAVEVIEWER_MAX_TEXTURE_SIZE` can set an
explicit maximum texture dimension in pixels.

### Map Parsing

| Preference | Environment variable | Default | Valid range | What it changes |
|---|---|---:|---|---|
| Import chunk size (m) | — | 8 | greater than 0 and up to 512 | Spatial chunk size used when building new cache data. |
| OBJ scan throttle (ms) | — | 0 on macOS/Linux, 1 on Windows | 0 to 50 ms | Yield/throttle behavior during OBJ scanning. |
| Cache-building worker limit | — | 1 | integer, 1 to 32 | Chunk-cache writer limit. Worker count may be lower when CPU or RAM is constrained. |
| Cache-build CPUs to keep free | — | 2 | integer, 2 to 32 | CPU cores reserved during cache building. |

### Recordings

| Preference | Environment variable | Default | Valid range | What it changes |
|---|---|---:|---|---|
| Recordings folder | `CAVEVIEWER_RECORDING_DIR` | `~/Movies/CaveViewer` | writable folder, or a folder that can be created | Where saved recordings are stored. |

### Streaming vs Chunking: What Changes Now vs Later

Use this rule of thumb:

- Chunking settings affect how cache chunks are built during import and apply only to new imports (or a rebuilt cache).
- Streaming settings affect runtime behavior and can be changed for any map at any time.

In practice:

- Adjust Chunk uploads per frame, Upload budget (ms), and memory targets to tune smoothness without re-importing.
- Adjust Import chunk size (m) when you want a different chunk layout, then rebuild/import the map to test it.

### If You See an Out-of-Memory Error

Large photogrammetry maps can exceed the practical memory available on a 16 GB computer, especially while CaveViewer is importing a new map or decoding textures. Start with conservative settings, then increase them only after the map opens reliably.

In Preferences, try this first:

| Setting | Safer value |
|---|---:|
| System RAM target (%) | 6 to 8 |
| GPU memory target (%) | 40 to 50 |
| Loading worker limit | 1 or 2 |
| Loading CPUs to keep free | 3 or 4 |
| Chunk uploads per frame | 1 |
| Upload budget (ms) | 1 to 3 |
| Cache-building worker limit | 1 |
| Cache-build CPUs to keep free | 3 or 4 |

If the error happens while importing a map, lower Cache-building worker limit first. The cache-building worker pool can temporarily hold more geometry and texture data in memory, so one worker is the safest option on a constrained machine.

If the error happens while moving around an already-imported map, lower System RAM target (%) and GPU memory target (%). CaveViewer may also downscale oversized textures automatically when the texture set is too large for the GPU budget.

If the map still cannot import, rebuild the cache with a larger Import chunk size such as 16 m, 24 m, or 32 m. Larger chunks reduce total chunk count and bookkeeping overhead, but do not push this too high on a 16 GB machine because each chunk becomes heavier to load.

Close other memory-heavy applications before importing. Browsers, photo tools, video editors, and other 3D apps can leave too little headroom for a large model even when the computer reports 16 GB of installed RAM.

### Recommended Map Parsing Approach

On Linux, imports use a self-contained cache under
`~/.cache/caveviewer/maps/<map-name>-<path-hash>` by default. CaveViewer no
longer auto-discovers old `_cache` or `.caveviewer_cache` folders beside a map.
Set `CAVEVIEWER_MAP_CACHE_DIR` to an absolute path when large caches belong on
another filesystem. The log reports the exact cache directory selected for each
import.

1. Understand your map first.
Decide how far ahead you need to see while moving. Long, open passages often benefit from larger chunk sizes. Maps with many twists and short sightlines may not need very large chunks, especially on strong hardware.

2. Understand your hardware limits.
Check what you have available: GPU memory, CPU cores, and system RAM. More hardware headroom usually allows higher upload budgets and more aggressive streaming.

3. Start with stable streaming settings.
Begin with Chunk uploads per frame = 1 and Upload budget = 2 to 4 ms. This usually gives smoother frame pacing while you evaluate map behavior.

4. Test chunking approaches for that specific map.
Try a few Import chunk size values (for example 16, 32, 64, then 100 m for very large/open maps). Rebuild/import each time so the new chunk layout is actually used. Use separate `CAVEVIEWER_MAP_CACHE_DIR` roots when you want to retain multiple managed-cache experiments side by side.

5. Tune streaming after choosing a chunk size.
If pop-in is too visible, raise Chunk uploads per frame gradually (1, then 2, then 3) and increase Upload budget carefully (for example from 3 to 5 ms).

6. Balance quality and stability.
If you see memory pressure, lower System RAM target (%) and GPU memory target (%). If import is slow but runtime is fine, increase Cache-building worker limit or reduce Cache-build CPUs to keep free.

Important: there is no single best value for all maps. The best result comes from trying several import strategies and streaming settings for your map and your hardware.

## Contributing

See [`CONTRIBUTING.md`](CONTRIBUTING.md) for the change workflow and
[`docs/development/`](docs/development/README.md) for architecture, repository
layout, coding, testing, and AI-assisted development standards.

## License

CaveViewer is free software licensed under the GNU General Public License version 3.0. See `LICENSE` for the full license text.

Third-party dependency notices are listed in `THIRD_PARTY_NOTICES.md`.
