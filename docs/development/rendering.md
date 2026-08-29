# CaveViewer Import and Streaming Rendering

This document describes how CaveViewer turns very large photogrammetry maps into
something that can be explored interactively, and how to tune that behavior on
low-memory or unreliable-GPU systems.

## Rendering philosophy

CaveViewer does not try to render a source model as one giant mesh. First-time
import compiles the source map into a generated cache made of spatial chunks
and staged texture assets. By default that cache lives in `_cache` inside the
source map folder. Runtime rendering then keeps only the nearby working set
resident, uploads new GPU resources in bounded slices, and unloads old chunks as
the camera moves.

The main goals are:

- keep geometry visible even when the original texture set is too large for the
  GPU;
- avoid long render-thread stalls by doing disk reads, geometry preparation, and
  texture decode work off the render thread;
- limit render-thread GPU upload work per frame;
- prefer conservative defaults that work on laptops and integrated GPUs, while
  still allowing higher-end machines to stream more aggressively.

## High-density presentation

The viewer requests four samples of multisample anti-aliasing for the default
presentation framebuffer. This smooths the cave scene and all OpenGL HUD
overlays shown while a map opens, a cache compiles, or a capture begins, ends,
or is cancelled. The shared progress-ring shader also derives its edge width
from framebuffer pixels, so its circular indicator stays smooth when a display
uses a high scaling factor. The minimap uses a denser round-marker mesh for the
same reason.

Tk splash and Map Library canvases use bounded supersampled Pillow images for
their circular indicators, chevrons, disclosure triangles, and other curved
or diagonal icons. FreeType text, source imagery, OS-native controls, and
axis-aligned panels and stop/pause glyphs already scale cleanly and remain on
their normal paths. These presentation changes do not change generated map
caches, capture workflow, or recorded artifact output.

Chunking and streaming are separate decisions:

- Import/chunking settings affect the cache layout and apply only to new or
  rebuilt caches.
- Streaming settings affect runtime loading and GPU upload behavior and can be
  changed without rebuilding the map.

## Import model

When you open a new OBJ or GLB map, CaveViewer builds a self-contained cache.
The cache contains chunk files, manifest metadata, and any staged texture assets
needed by the map. The cache is published atomically as `_cache` inside the map
folder by default, so interrupted imports do not leave a half-valid cache
visible to normal launches.

Map Library's `Rebuild cache` action uses the same importer with the current
Import preferences without opening a viewer. It is available only for an
existing generated cache with a readable source and a safe cache target, and
stages its replacement so the older cache remains usable until publication
succeeds.

Chunk size is the most important import-time rendering choice:

- Larger chunks reduce total chunk count and can be smoother in long/open
  passages, but each chunk costs more to upload and keep resident.
- Smaller chunks provide finer-grained culling and loading in tight/twisty
  passages, but can increase chunk churn and bookkeeping.

Dense material groups can also be split during import with the max upload group
size. This limits a single VBO payload, which matters for frame pacing during
runtime upload. The renderer also slices oversized VBO uploads at runtime, so
existing caches get bounded upload units even before they are rebuilt.

## Streaming model

At runtime, CaveViewer selects wanted chunks around the camera, loads chunk data
on background workers, pre-packs vertex bytes, pre-decodes textures where memory
allows, and then hands ready chunks to the render thread.

The render thread owns OpenGL resources, so VBOs, VAOs, and textures must still
be created there. CaveViewer limits that work in two layers:

- chunk upload throttling: how many ready chunks may advance per frame;
- render-upload operation throttling: how many resumable upload slices from one
  ready chunk may advance per frame.

Texture uploads are resumable: workers decode images into CPU bytes, then the
render thread first allocates the OpenGL texture, uploads row bands across
multiple frames, and builds mipmaps only after the final row band. Dense
geometry uploads are also split into triangle-aligned VBO slices, with buffer
storage reservation and data writes advanced as separate operations where the
OpenGL context supports it. These operation-level upload queues preserve final
texture quality while avoiding one large `ctx.texture(...)` or
`ctx.buffer(...)` call monopolizing a frame.

Upload slice sizes are self-adjusting at runtime. CaveViewer starts with
conservative 512 KiB texture and VBO upload slices and can shrink future slices
down to 256 KiB when a measured OpenGL upload operation exceeds the active
frame upload budget. This keeps normal quality settings usable on drivers where
even a 1 MiB upload can occasionally stall the render thread without degrading
into tiny fragment uploads after one bad frame.

Startup uses a separate pacing rule. While the fullscreen loading/controls
overlay is still hiding the map, the viewer advances more render-thread upload
operations per frame and waits for the current render-distance wanted set to be
GPU-ready before enabling the "Press Space to begin" prompt. After the user
begins, streaming normally returns to conservative per-frame limits. If the
current Distance wanted set is incomplete and decoded chunks are ready, the
viewer temporarily enters a catch-up upload mode so nearby visible chunks fill
in coherent sections instead of crawling one small slice per frame.

The Distance control defines the current visual wanted set. Memory-derived
residency limits do not silently shrink that set; instead, bounded worker queues
and ready backlogs limit how much work can accumulate while eviction prefers
cells outside the requested Distance range.

Completed upload slices become drawable before the entire chunk finishes.
Partial GPU resources are tracked separately and released if the chunk becomes
stale before completion.

The render loop frustum-culls resident chunks before drawing them. The culling
result is cached across frames and invalidated when the camera view/projection
changes or when the resident chunk generation changes. This keeps static/idling
views from retesting every loaded chunk every frame while preserving immediate
updates as chunks stream in and out.

Texture handling is intentionally separate from geometry residency. If a map has
too many large texture tiles for the GPU budget, CaveViewer downsizes oversized
textures during decode instead of dropping nearby geometry from the visible
world.

## Low-memory and VM recommendations

Start conservative when testing a constrained machine, VM, integrated GPU, or
unreliable graphics driver:

```bash
CAVEVIEWER_MEMORY_UTILIZATION_TARGET=6 \
CAVEVIEWER_GPU_MEMORY_UTILIZATION_TARGET=50 \
CAVEVIEWER_UPLOAD_CHUNKS_PER_FRAME=1 \
CAVEVIEWER_UPLOAD_GROUPS_PER_FRAME=1 \
CAVEVIEWER_UPLOAD_TIME_BUDGET_MS=2 \
./run_caveviewer.sh
```

Windows PowerShell equivalent from a source checkout:

```powershell
$env:CAVEVIEWER_MEMORY_UTILIZATION_TARGET = "6"
$env:CAVEVIEWER_GPU_MEMORY_UTILIZATION_TARGET = "50"
$env:CAVEVIEWER_UPLOAD_CHUNKS_PER_FRAME = "1"
$env:CAVEVIEWER_UPLOAD_GROUPS_PER_FRAME = "1"
$env:CAVEVIEWER_UPLOAD_TIME_BUDGET_MS = "2"
.\.venv\Scripts\python -m caveviewer
```

If you are using the Windows setup package instead of a development venv, its
Desktop shortcut already uses CaveViewer's verified runtime. For a temporary
PowerShell override, use the exact `python.exe` path recorded in the setup log
under `%LOCALAPPDATA%\CaveViewer\logs`, then run `-m caveviewer` from the
extracted CaveViewer folder. PowerShell environment variables apply to the
current shell session; close the window or run `Remove-Item Env:\NAME` to clear
one.

### GPU driver instability

If CaveViewer crashes, freezes, or leaves a stuck process when using an
unreliable Mesa/OpenGL driver, first disable vsync:

```bash
CAVEVIEWER_VSYNC=0 ./run_caveviewer.sh
```

For an AppImage launch, use the same prefix with `./CaveViewer-*.AppImage`.

Windows PowerShell equivalent from a source checkout:

```powershell
$env:CAVEVIEWER_VSYNC = "0"
.\.venv\Scripts\python -m caveviewer
```

If you are using the Windows setup package instead of a development venv, use
the Desktop shortcut for normal launches. For an explicit PowerShell launch,
use the verified `python.exe` path recorded in the setup log under
`%LOCALAPPDATA%\CaveViewer\logs`, from the extracted CaveViewer folder:

```powershell
$env:CAVEVIEWER_VSYNC = "0"
python -m caveviewer
```

If the GPU driver still hangs, force software OpenGL rendering as a stronger
fallback on Linux/Mesa:

```bash
LIBGL_ALWAYS_SOFTWARE=1 \
CAVEVIEWER_VSYNC=0 \
CAVEVIEWER_UPLOAD_CHUNKS_PER_FRAME=1 \
CAVEVIEWER_UPLOAD_GROUPS_PER_FRAME=1 \
CAVEVIEWER_UPLOAD_TIME_BUDGET_MS=1 \
./run_caveviewer.sh
```

`LIBGL_ALWAYS_SOFTWARE=1` tells the Linux OpenGL stack to use software rendering
instead of the GPU driver. This is slower, but it can avoid crashes or
kernel-level hangs caused by unstable graphics drivers.

Windows does not use `LIBGL_ALWAYS_SOFTWARE`. For a stronger Windows stability
profile, keep vsync disabled and lower render-thread upload work:

```powershell
$env:CAVEVIEWER_VSYNC = "0"
$env:CAVEVIEWER_UPLOAD_CHUNKS_PER_FRAME = "1"
$env:CAVEVIEWER_UPLOAD_GROUPS_PER_FRAME = "1"
$env:CAVEVIEWER_UPLOAD_TIME_BUDGET_MS = "1"
.\.venv\Scripts\python -m caveviewer
```

For the Windows setup package, use the same `$env:` values with the verified
`python.exe` path recorded in the setup log, rather than an ambient `python`
command from PATH.

Practical tuning order:

1. Keep `CAVEVIEWER_UPLOAD_CHUNKS_PER_FRAME=1` and
   `CAVEVIEWER_UPLOAD_GROUPS_PER_FRAME=1` until frame pacing is stable.
2. Use an upload budget around 1-3 ms on constrained systems, 2-5 ms on typical
   desktops, then increase only if pop-in is too visible.
3. Lower system/GPU memory targets before lowering visual range.
4. Set `CAVEVIEWER_GPU_MEMORY_GB` explicitly when auto-detection is unavailable
   or wrong.
5. Use `CAVEVIEWER_MAX_TEXTURE_SIZE` only for debugging or hard caps; automatic
   texture sizing is usually preferable.
6. If import itself fails, reduce cache-build workers or rebuild with a different
   chunk size. Larger chunks reduce chunk count but make each chunk heavier, so
   test a few values instead of assuming one global best setting.

## CLI launch parameters for runtime rendering

The GUI viewer does not use rendering-specific command-line flags. Runtime
rendering is configured through Preferences or environment variables supplied at
launch. On macOS/Linux, set environment variables before launching, or prefix
them inline:

```bash
CAVEVIEWER_UPLOAD_GROUPS_PER_FRAME=1 CAVEVIEWER_UPLOAD_TIME_BUDGET_MS=2 ./run_caveviewer.sh
```

PowerShell does not support POSIX-style inline environment assignment. Set the
variables first, then launch CaveViewer:

```powershell
$env:CAVEVIEWER_UPLOAD_GROUPS_PER_FRAME = "1"
$env:CAVEVIEWER_UPLOAD_TIME_BUDGET_MS = "2"
.\.venv\Scripts\python -m caveviewer
```

| Variable | Default | Range | Purpose |
|---|---:|---|---|
| `CAVEVIEWER_MEMORY_UTILIZATION_TARGET` | `8` | 1-80% | Percentage of available system RAM targeted for loaded chunk data. |
| `CAVEVIEWER_GPU_MEMORY_GB` | auto | 0.5-50 GB | Optional GPU memory ceiling. If active-GPU detection finds a smaller budget, the detected value wins. |
| `CAVEVIEWER_GPU_MEMORY_UTILIZATION_TARGET` | `70` | 1-80% | Percentage of GPU memory targeted for combined texture and geometry streaming residency. |
| `CAVEVIEWER_TEXTURE_RESIDENT_CACHE_MB` | auto | positive MB | Optional upper bound for the GPU texture-residency LRU cache. It can lower, but never raise, the automatic limit; use it for constrained GPUs or diagnostic runs. |
| `CAVEVIEWER_MAX_TEXTURE_SIZE` | auto | 512-16384 px | Optional maximum decoded texture dimension. Automatic sizing usually gives better balance. |
| `CAVEVIEWER_IO_WORKERS` | `2` | 1-32 workers | Requested maximum background chunk-loading workers. Runtime starts conservatively and grows only when RAM allows. |
| `CAVEVIEWER_IO_NICE` | `5` | 0+ | Positive Linux per-thread nice increment for chunk-loading workers; `0` disables it. The GUI/render thread is not changed. |
| `CAVEVIEWER_IMPORT_NICE` | `5` | 0+ | macOS/Linux only: positive nice increment used to lower the spawned cache-import process priority; `0` leaves its priority unchanged. Windows always requests below-normal import priority. |
| `CAVEVIEWER_IO_RESERVED_CPUS` | `3` | 2-32 logical CPUs | Logical CPUs kept out of the loading pool. |
| `CAVEVIEWER_UPLOAD_CHUNKS_PER_FRAME` | `1` | 1-16 chunks | Base maximum ready chunks advanced by the render thread per frame. Startup and catch-up modes can temporarily raise this. |
| `CAVEVIEWER_UPLOAD_GROUPS_PER_FRAME` | `1` | 1-64 operations | Base maximum render-thread upload slices advanced from one ready chunk per frame. Startup and catch-up modes can temporarily raise this. |
| `CAVEVIEWER_UPLOAD_TIME_BUDGET_MS` | `3.0` | 0.5-50 ms | Base soft per-frame target for render-thread upload work. Startup and catch-up modes can temporarily raise this. |
| `CAVEVIEWER_GPU_DRAW_TIMER` | `0` | 0 or 1 | Enable same-frame OpenGL GPU draw timer queries in frame diagnostics. This can block on some drivers, so leave it disabled during normal viewing. |
| `CAVEVIEWER_VSYNC` | `1` | 0 or 1 | Disable with `0` when diagnosing display-driver stalls. |
| `LIBGL_ALWAYS_SOFTWARE` | unset | `1` on Linux/Mesa | Force software OpenGL rendering when GPU drivers are unstable. |

GPU memory detection currently uses Linux DRM sysfs for AMD GPUs and
`nvidia-smi` for NVIDIA GPUs. Low-VRAM AMD integrated GPUs include a conservative
shared-memory allowance: 50% of reported GTT/shared memory, capped at 2 GB.
Windows AMD/Intel GPU memory is not currently auto-detected and uses an 8 GB
fallback budget; macOS uses a conservative 1 GB fallback.

## Cache import CLI

Use `caveviewer-chunker` when you want to compile or rebuild a map cache without
launching the GUI.

```bash
caveviewer-chunker --source=/path/to/map-or-folder --chunk-size=64
```

From a source checkout, the module entry point is equivalent:

```bash
.venv/bin/python -m caveviewer.chunker --source=/path/to/map-or-folder --chunk-size=64
```

Windows PowerShell examples:

```powershell
caveviewer-chunker --source="C:\Maps\DevilsEye.obj" --chunk-size=64
```

```powershell
.\.venv\Scripts\python -m caveviewer.chunker `
  --source="C:\Maps\DevilsEye" `
  --cache-root="D:\CaveViewer\maps" `
  --chunk-size=64 `
  --json
```

Normal cache compilation publishes only render assets, render chunks, the
manifest, and the Guided Dive identity. Its final progress phases explicitly
show render-manifest assembly, Guided Dive identity hashing, and publication.
It does not generate additional route-planning artifacts.

| Option | Purpose |
|---|---|
| `--source=<path>` | Required. OBJ file, GLB file, or folder containing a map. |
| `--cache-root=<path>` | Optional absolute root where hashed compiled map caches are stored. Defaults to `_cache` inside the source map folder. |
| `--settings-file=<path>` | Preferences JSON to use for import defaults. Saved GUI Preferences are not loaded by default. |
| `--chunk-size=<value>` | Import chunk size for new or rebuilt caches. |
| `--max-upload-group-mb=<value>` | Maximum VBO payload size for dense chunk groups, in MB. |
| `--obj-scan-throttle-ms=<value>` | Milliseconds paused while scanning OBJ files. |
| `--obj-import-batch-thousands=<n>` | Thousands of triangulated OBJ faces processed per batch. |
| `--obj-bucket-workers=<n>` | Worker threads used for temporary OBJ bucket files. |
| `--chunk-build-workers=<n>` | Cache-building worker limit. |
| `--chunk-build-reserved-cpus=<n>` | Logical CPUs kept free during cache build. |
| `--analyze-chunk-sizes` | Analyze source geometry and recommend a chunk size without building. |
| `--analyze-workers=<n>` | Worker threads used by chunk-size analysis. |
| `--force` | Rebuild even if a valid matching cache already exists; an active build for that cache is rejected. |
| `--dry-run` | Validate inputs and print the planned cache path. |
| `--json` | Print machine-readable output. |
| `-h`, `--help` | Show CLI help. |

Built-in import defaults:

| Import setting | Default |
|---|---:|
| `--chunk-size` / `CAVEVIEWER_CHUNK_SIZE_METERS` | `50` |
| `--max-upload-group-mb` / `CAVEVIEWER_MAX_UPLOAD_GROUP_MB` | `16` |
| `--obj-scan-throttle-ms` / `CAVEVIEWER_OBJ_SCAN_THROTTLE_MS` | `0` on Linux/macOS, `1` on Windows |
| `--obj-import-batch-thousands` / `CAVEVIEWER_OBJ_IMPORT_BATCH_FACES` | `200` thousand faces |
| `--obj-bucket-workers` / `CAVEVIEWER_OBJ_BUCKET_WORKERS` | `2` |
| `--chunk-build-workers` / `CAVEVIEWER_CHUNK_BUILD_WORKERS` | `1` |
| `--chunk-build-reserved-cpus` / `CAVEVIEWER_CHUNK_BUILD_RESERVED_CPUS` | `2` |

Cache location defaults:

- GUI and CLI imports write generated cache files to `_cache` inside the source
  map folder.
- `CAVEVIEWER_MAP_CACHE_DIR` and CLI `--cache-root` are advanced overrides for
  placing hashed generated caches under a separate absolute root.
- The older `.caveviewer_cache` directory is not auto-discovered.

PowerShell session-only cache override:

```powershell
$env:CAVEVIEWER_MAP_CACHE_DIR = "D:\CaveViewer\maps"
python -m caveviewer
```

Persistent user-level cache override:

```powershell
[Environment]::SetEnvironmentVariable("CAVEVIEWER_MAP_CACHE_DIR", "D:\CaveViewer\maps", "User")
```
