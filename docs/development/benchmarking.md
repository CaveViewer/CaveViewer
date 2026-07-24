# Viewer benchmarking

Viewer benchmarks protect streaming/rendering quality between releases. They
measure frames per second against a deterministic camera route over a
precompiled chunk cache, then compare the candidate build with a baseline build.

## Scope

The benchmark intentionally starts from an existing chunk cache. Import and
chunk compilation are important, but they are not FPS-streaming behavior. Keep
those covered by the normal tests and package smoke workflows.

The benchmark records:

- `summary.json`: aggregate FPS/frame-time metrics and pass/fail inputs.
- `frames.jsonl`: one JSON object per measured frame.
- `environment.json`: app, Python, platform, OpenGL, runner, and tuning details.
- `benchmark.log` and workflow `console.log`: parse-friendly debug output.

Each summary includes both user-visible wall-clock FPS and lower-level render
work timing. Use `wall_clock_fps` as the primary release gate: it is frames
completed during the benchmark measurement window divided by wall-clock
measurement seconds. The older `median_fps` value remains useful diagnostic
data, but it is derived from per-frame measured render-work time and can be much
higher than what a user actually sees when frame intervals include scheduling,
upload waits, or other loop overhead.

Each summary includes a scenario fingerprint and a SHA-256 of the cache
`manifest.json`. The comparison step fails if either differs between baseline
and candidate. That prevents accidental release decisions from comparing a
different route or a different benchmark map.

## Existing-cache run

Install the project in editable mode, then run against a precompiled map cache:

```bash
caveviewer-benchmark \
  --cache-dir /path/to/precompiled-cache \
  --textures-dir /path/to/precompiled-cache \
  --scenario benchmarks/viewer-benchmark-scenario.v1.json \
  --output-dir benchmark-results/current \
  --log-level DEBUG
```

The default benchmark mode disables vsync by setting `CAVEVIEWER_VSYNC=0` before
the OpenGL window module is imported. Use `--vsync unchanged` only when you are
intentionally measuring a display-synchronized path.

For local maps that need cache creation, route generation, history, and a text
summary, use the generic local map runner instead:

```bash
caveviewer-map-benchmark \
  --map-dir /path/to/local/map \
  --dry-run
```

Then run it:

```bash
caveviewer-map-benchmark \
  --map-dir /path/to/local/map
```

The local map runner stores history in `<map-dir>/_benchmarks` by default and
compares the current result with the latest compatible previous local run. It
does not switch git branches. To compare `main` with a candidate checkout, run
the same map benchmark from separate worktrees and let the map-local history
provide the baseline.

Compare two runs:

```bash
python scripts/benchmark/compare_benchmark_results.py \
  --baseline benchmark-results/baseline/summary.json \
  --candidate benchmark-results/current/summary.json \
  --output benchmark-results/comparison.json \
  --thresholds benchmarks/viewer-thresholds.v1.json
```

## Benchmark package and entry points

`caveviewer.benchmarking` is a library package, not a direct command-line
entry point. Keep benchmark-only code there when it does not need Tk, OpenGL,
or viewer presentation:

- `caveviewer.benchmarking.results`: scenario parsing, frame samples,
  summaries, thresholds, comparisons, and the benchmark controller.
- `caveviewer.benchmarking.routes`: benchmark-specific route generation and
  load-focused route selection.
- `caveviewer.benchmarking.map_runner`: local map benchmark orchestration
  exposed as `caveviewer-map-benchmark`.

Run benchmarks through one of the supported entry points:

- `caveviewer-map-benchmark` for local map directories. This is
  the recommended path for private or oversized benchmark maps because it can
  validate/build `_cache`, generate a route, run the viewer, record local
  history, and write a human-readable summary.
- `python -m caveviewer.benchmark` or `caveviewer-benchmark` for direct runs
  against an existing cache and scenario JSON.
- `scripts/benchmark/compare_benchmark_results.py` for comparing two existing
  `summary.json` files without launching the viewer.

Do not run `python -m caveviewer.benchmarking.results`; it has no CLI contract.
Import it from tests, scripts, or viewer adapters instead.

## Generic local map run

Use `caveviewer-map-benchmark` for machine-local benchmark maps that are too
large or private to upload. The runner takes a map directory from CLI flags or
from a JSON config file. By default it writes benchmark artifacts next to that
map under `<map-dir>/_benchmarks`, which is ignored by Git. The map directory
must contain either:

- `_cache/manifest.json`; or
- a supported source model (`.obj`, `.glb`, or `.gltf`) that the runner can
  compile with `python -m caveviewer.chunker --source <map-dir>`.

If an existing editable environment was installed before
`caveviewer-map-benchmark` existed, refresh the dev install or run the equivalent
module form:

```bash
python -m caveviewer.benchmarking.map_runner --help
```

Minimal CLI run:

```bash
caveviewer-map-benchmark \
  --map-dir /path/to/local/map
```

Minimal config file:

```json
{
  "map_dir": "/absolute/path/to/local/map",
  "render_distance": 10,
  "duration_seconds": 120,
  "centerline_route_target_length_chunks": 24,
  "centerline_route_selection": "max-complexity",
  "vsync": "unchanged"
}
```

First validate the plan:

```bash
caveviewer-map-benchmark \
  --config /path/to/local-benchmark.json \
  --dry-run
```

Then run it:

```bash
caveviewer-map-benchmark \
  --config /path/to/local-benchmark.json
```

CLI flags override config values:

```bash
caveviewer-map-benchmark \
  --map-dir /path/to/local/map \
  --render-distance 10 \
  --duration-seconds 120 \
  --centerline-route-selection max-complexity \
  --vsync unchanged
```

Use `--output-dir /path/to/benchmark-output` only when you want artifacts
outside the map directory. Use `--benchmark-id <id>` only when you deliberately
want to override the default identity.
Use `--scenario-template` or the `scenario_template` config key when selecting
a different timing/control template for generated map routes. The older
`--scenario` option and `scenario` config key remain supported aliases.

The runner clears `CAVEVIEWER_MAP_CACHE_DIR` for the chunker and benchmark
subprocesses so the benchmark uses the map-local cache. Each run tees runner,
chunker, and benchmark output into that run's `orchestration.log`. Each
successful benchmark also appends `<map-dir>/_benchmarks/history.jsonl` and
writes `<map-dir>/_benchmarks/latest-summary.txt` unless `--output-dir` is set.
The default `benchmark_id` is the SHA-256 of `_cache/manifest.json` after cache
validation, so rebuilding or replacing the map cache creates a new local map
identity. When a previous compatible local record
exists, the new run is compared against it with
`benchmarks/viewer-thresholds.v1.json`; a regression returns a non-zero exit
code and still leaves all artifacts for inspection.

The console output and `latest-summary.txt` include the current metrics, the
single latest run used as the gate baseline, and a simple wall-clock FPS
comparison against the previous local run:

```text
Wall FPS comparison (higher is better):
  Current  [###########################---] 90.00 fps
  Previous [##############################] 100.00 fps (previous-main)
  Delta: -10.00%
```

Set `--history-limit 0` when deliberately hiding that previous-run comparison.
Detailed threshold checks are still written to `comparison.json`.

The benchmark viewer launches through the same window-sizing path as the
regular CaveViewer app: 80% of the detected desktop/work area using the active
GLFW backend and DPI coordinate policy. The scenario's `window_size` remains in
the JSON for historical compatibility, but it is not used to force the local
benchmark viewer window. The actual logical window size, framebuffer size, and
UI surface size are recorded in the run environment and text summary; runs with
different actual sizes are treated as incompatible local baselines.

For an existing `_cache`, the runner matches the regular app's cache-open path
by using `<map-dir>/_cache` as both the chunk cache and texture root. That keeps
texture resolution aligned with maps compiled by `caveviewer.chunker`.

Before timing starts, benchmark mode prefetches the generated route tube using
the benchmark render distance. Startup readiness waits for those route cells and
their textures to become resident, so a route should not begin with an unloaded
black void waiting at the far end. The run summary reports this as
`Route prefetch: loaded=<n>/<n> cells`.

By default the local runner generates an `auto-centerline-route-v1.json` file
inside the run artifact directory after the cache exists. It uses the
fine-grained `footprint_cells` manifest field, which the chunker derives from
source vertex positions, to estimate the middle of the cave passage. The
generator first computes a reusable high-clearance centerline path through that
footprint, where clearance means grid distance from the footprint boundary.
That centerline step is separate from benchmark load selection and does not
need texture or chunk-complexity data.

The shared centerline and camera-route primitives live under
`caveviewer.core.navigation`. Keep generic navigation behavior there so future
viewer features, such as an opt-in autopilot dive, can reuse it without pulling
in benchmark measurement, threshold, or artifact-writing code. Keep benchmark
load scoring and scenario metadata in `caveviewer.benchmarking.routes`.

The default benchmark selector then scores candidate centerline positions by
render-distance forward-view load from the route camera direction: visible chunk
count plus unique texture count from the manifest material-to-texture map. This
avoids selecting a heavy area that is mostly behind or beside the camera. The
route segment is centered on the highest-scoring position instead of sweeping
the entire cave. Use `--centerline-route-selection midpoint` to move through
the centerline midpoint without using chunk or texture complexity for segment
selection.

The generated route holds the first camera pose through warmup, then travels
only during the measurement window. The default measured window is 120 seconds
at render distance 10, and the default travel distance is 24 chunk widths. For
a 50-meter chunk cache, that is about 1.2 km over the measured 120 seconds:
enough to exercise chunk loading/unloading and texture residency without
intentionally outrunning the streamer. It estimates Y from the nearest occupied
chunk columns by placing the camera at the midpoint of the local min/max chunk
bounds, making tall passages more likely to stream and show ceiling geometry.

This remains a deterministic benchmark route, not a collision-checked
navigation mesh. The cache describes surface geometry and chunk bounds; it does
not encode free water volume or diver-swimmable constraints. The generator
therefore uses footprint clearance as a proxy and pushes low-clearance sampled
route points toward the selected centerline cell centers, but that is not the
same as mesh collision.

Use these controls when calibrating the route:

```bash
caveviewer-map-benchmark \
  --config /path/to/local-benchmark.json \
  --duration-seconds 180 \
  --centerline-route-selection midpoint \
  --centerline-route-keyframes 10 \
  --centerline-route-target-length-m 1000
```

Use `--duration-seconds` (or its aliases `--duration` and
`--measurement-seconds`) when you want a longer or shorter measured window.
Use `--centerline-route-selection midpoint` when validating generic centerline
movement instead of the load-focused benchmark segment. The default
`max-complexity` selection preserves the performance-regression target.
Use `--centerline-route-target-length-m` only when deliberately recalibrating
the route. Overriding duration or route length changes the scenario fingerprint
and local comparison history for that route.

To run the older dense-streaming-load proxy instead:

```bash
caveviewer-map-benchmark \
  --config /path/to/local-benchmark.json \
  --route-mode auto-dense \
  --dense-route-keyframes 10 \
  --dense-route-percentile 85
```

To run the checked-in scenario exactly as written instead:

```bash
caveviewer-map-benchmark \
  --config /path/to/local-benchmark.json \
  --route-mode static
```

To make this run automatically before local pushes to `main`, install the
tracked pre-push hook template on this machine:

```bash
ln -sf ../../scripts/benchmark/hooks/pre-push-map-benchmark .git/hooks/pre-push
```

The hook only runs when the destination ref is `refs/heads/main`. It uses
`$PYTHON` when set, otherwise `.venv-dev/bin/python` when present, then falls
back to `python3`. Set `CAVEVIEWER_BENCHMARK_CONFIG` to the local JSON config
path before pushing.

## Scenario file

Scenario files are versioned JSON.
`benchmarks/viewer-benchmark-scenario.v1.json` is the default viewer benchmark
scenario. Direct benchmark runs use its route as written. Generic map runs use
it as a timing/control template, then write a generated map-specific scenario
inside the run artifact directory. The default file uses
`position_mode: "first_chunk_center_offset"` so it can run on any precompiled
cache.

Keep scenario changes reviewable. A changed route changes the benchmark itself,
so compare old and new thresholds deliberately instead of mixing results from
different scenarios.

## Threshold file

`benchmarks/viewer-thresholds.v1.json` is the default threshold policy for local
and workflow comparisons. Keep threshold changes separate from route changes
where practical: a route change invalidates baseline data, while a threshold
change changes release policy.

The comparison script accepts one-off numeric overrides for calibration, but
routine checks should use the tracked threshold file so local and GitHub Actions
runs agree.

## CI workflow

Run `.github/workflows/viewer-benchmark.yml` manually from GitHub Actions.

Required production input:

- `benchmark_map_url`: an HTTPS URL to a zip that contains the precompiled chunk
  cache.

Recommended inputs:

- `benchmark_map_sha256`: expected SHA-256 for the zip.
- `benchmark_cache_subdir`: subdirectory inside the zip that contains
  `manifest.json`; leave as `.` when the zip root is the cache.
- `baseline_ref`: `main` for PR-style checks, or the previous release tag for a
  release comparison.
- `candidate_ref`: leave empty for the workflow SHA, or provide a branch/tag/SHA.
- `threshold_config_path`: threshold JSON from the candidate checkout; defaults
  to `benchmarks/viewer-thresholds.v1.json`.
- `runner_label`: use a stable GPU/display runner when available.

When `benchmark_map_url` is omitted, the workflow exits successfully after
printing setup guidance. This keeps the workflow available before a benchmark
map artifact is published without creating noisy CI failures.

## Calibration policy

Do not make first-run benchmark numbers release-blocking. Calibrate on the same
runner class with the same benchmark map:

1. Run the current release or `main` three or more times.
2. Inspect `summary.json`, `frames.jsonl`, and `benchmark.log` for warmup,
   shader/cache, or runner instability.
3. Set thresholds after observing normal variance.
4. Treat failures as release blockers only after the map artifact and runner
   class are stable.

Use the default comparison limits as starting values: 5% wall-clock FPS drop,
5% median render-FPS drop, 10% 1% low render-FPS drop, 15% p95 frame-time
increase, and 20% stutter-count increase.
