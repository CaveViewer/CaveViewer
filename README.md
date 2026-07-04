# CaveViewer

CaveViewer makes exploring massive 3-D maps accessible to everyone. Designed for cave divers, explorers, cartographers, and 3-D mapping enthusiasts, it uses an innovative rendering technique to display extremely large maps on anything from lightweight laptops to high-end workstations. Released as open-source software under the GNU GPL v3 license, CaveViewer is completely free—no advertisements, no subscriptions, no online accounts, and no hidden costs.

CaveViewer supports several common 3-D map formats, including OBJ models exported from Agisoft Metashape, GLB files, and previously optimized CaveViewer cache files.

No matter which format you open, the experience is the same. CaveViewer automatically prepares the map for viewing, allowing you to explore even extremely large 3-D maps with the same navigation, rendering, and tools regardless of the original file format.

## How to Install and Run CaveViewer

## Supported Platforms

CaveViewer is currently available for the following platforms:

- macOS (Apple Silicon)
- Linux (Fedora and Ubuntu)
- Windows (10 and 11)

Support for additional operating systems and distributions may be added in the future based on community interest and user demand. If your preferred platform is not currently supported, we'd love to hear from you.

### macOS

Download the latest DMG from https://github.com/KernalPanic/CaveViewerPlus/releases, open it, and drag CaveViewer into Applications. 

If this is your first time installing the app, you have to go to Settings -> Privacy & Security and then allow the system to open CaveViewer. These steps are necessary because the app is not published through the App Store.

If there is an update, the app will let you download and install it.

NOTE: Intel Macs are not supported yet.

### Windows

Download the latest zip from https://github.com/KernalPanic/CaveViewerPlus/releases and extract it anywhere on your machine. Then click on `launch.bat` inside the folder to install. 

### Linux

The best-practice distribution format for Linux is the self-contained AppImage — a single executable file that bundles Python, all dependencies, and the app itself. No system-wide installation or package manager involvement required.

Download the AppImage matching your CPU from https://github.com/KernalPanic/CaveViewerPlus/releases:

- `CaveViewer-<version>-x86_64.AppImage` (amd64 / x86_64)
- `CaveViewer-<version>-aarch64.AppImage` (arm64)

The change the permissions to make the file executable and run.

```bash
chmod +x CaveViewer-*.AppImage

# Run the one that matches your architecture
./CaveViewer-*-x86_64.AppImage   # amd64 / x86_64
# or
./CaveViewer-*-aarch64.AppImage  # arm64
```

## Importing and Streaming Preferences

The Advanced Settings panel in the startup window acts as the advanced installer/preferences surface for import and streaming behavior. Open it from the splash screen with Advanced Settings....

These values are validated in the UI, applied to environment variables for the current launch, and saved to a local settings file so they are reused next time.

- Saved settings file: ~/.caveviewer_advanced_settings.json
- Streaming section controls runtime chunk loading and upload behavior.
- Map Parsing section controls cache-build/import behavior.

### Map Chunking

When you import a map, CaveViewer does not keep it as one giant object. It splits the map into many smaller 3D chunks and saves them in a cache. During viewing, CaveViewer loads only the chunks near you and unloads chunks farther away.

Why this matters: chunk size is one of the most important map settings because it strongly affects smoothness, pop-in, memory usage, and how far ahead the map can appear to load cleanly while you move.

- Larger chunks: fewer load/unload events, often smoother in long/open passages, but can increase per-chunk cost.
- Smaller chunks: finer-grained loading and culling, often useful in tight/twisty areas, but can increase chunk churn.


### Streaming Performance

| Preference | Environment variable | Default | Valid range | What it changes |
|---|---|---:|---|---|
| System RAM target (%) | — | 12 | 1 to 80 | Target share of total system RAM used for loaded chunks. |
| GPU memory target (%) | — | 70 | 1 to 80 | Target share of detected GPU memory used for loaded chunks. |
| GPU memory override (GB) | — | empty | greater than 0 and up to 1024 | Manual GPU memory size when auto-detection is unavailable or inaccurate. |
| Worker count | — | logical CPUs minus 3 (minimum 1) | integer, at least 1 | Number of background chunk-loading worker threads. |
| CPU cores to keep free | — | 3 | integer, at least 0 | Reserve CPU cores instead of using them for streaming workers. |
| Chunk uploads per frame | — | 1 | integer, 1 to 16 | Hard cap for how many ready chunks are uploaded each frame on the render thread. |
| Upload budget (ms) | — | 3.0 | 0.5 to 50.0 ms | Soft time budget per frame for chunk uploads. |

### Map Parsing

| Preference | Environment variable | Default | Valid range | What it changes |
|---|---|---:|---|---|
| Import chunk size (m) | — | 8 | greater than 0 and up to 512 | Spatial chunk size used when building new cache data. |
| OBJ scan throttle (ms) | — | 0 on macOS/Linux, 1 on Windows | 0 to 50 ms | Yield/throttle behavior during OBJ scanning. |
| Import worker count | — | logical CPUs minus 2 (minimum 1) | integer, at least 1 | Number of worker threads used while writing chunk cache files. |
| Import CPUs to keep free | — | 2 | integer, at least 0 | CPU cores reserved during cache build/import. |

### Streaming vs Chunking: What Changes Now vs Later

Use this rule of thumb:

- Chunking settings affect how cache chunks are built during import and apply only to new imports (or a rebuilt cache).
- Streaming settings affect runtime behavior and can be changed for any map at any time.

In practice:

- Adjust Chunk uploads per frame, Upload budget (ms), and memory targets to tune smoothness without re-importing.
- Adjust Import chunk size (m) when you want a different chunk layout, then rebuild/import the map to test it.

### Recommended Map Parsing Approach

Each imported map writes its chunk cache to an `_cache` folder inside that map directory. To try different import strategies for the same map (for example, different chunk sizes), you can either remove `_cache` and re-import, or rename it first (for example `_cache_32m`, `_cache_64m`) to preserve earlier results.

1. Understand your map first.
Decide how far ahead you need to see while moving. Long, open passages often benefit from larger chunk sizes. Maps with many twists and short sightlines may not need very large chunks, especially on strong hardware.

2. Understand your hardware limits.
Check what you have available: GPU memory, CPU cores, and system RAM. More hardware headroom usually allows higher upload budgets and more aggressive streaming.

3. Start with stable streaming settings.
Begin with Chunk uploads per frame = 1 and Upload budget = 2 to 4 ms. This usually gives smoother frame pacing while you evaluate map behavior.

4. Test chunking approaches for that specific map.
Try a few Import chunk size values (for example 16, 32, 64, then 100 m for very large/open maps). Rebuild/import each time so the new chunk layout is actually used. If you want to compare multiple versions side by side over time, rename `_cache` between imports to keep each result.

5. Tune streaming after choosing a chunk size.
If pop-in is too visible, raise Chunk uploads per frame gradually (1, then 2, then 3) and increase Upload budget carefully (for example from 3 to 5 ms).

6. Balance quality and stability.
If you see memory pressure, lower System RAM target (%) and GPU memory target (%). If import is slow but runtime is fine, increase Import worker count or reduce Import CPUs to keep free.

Important: there is no single best value for all maps. The best result comes from trying several import strategies and streaming settings for your map and your hardware.
