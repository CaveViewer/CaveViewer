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

## Local run

Install the project in editable mode, then run against a precompiled map cache:

```bash
caveviewer-benchmark \
  --cache-dir /path/to/precompiled-cache \
  --textures-dir /path/to/precompiled-cache \
  --scenario benchmarks/gold-route-v1.json \
  --output-dir benchmark-results/current \
  --log-level DEBUG
```

The default benchmark mode disables vsync by setting `CAVEVIEWER_VSYNC=0` before
the OpenGL window module is imported. Use `--vsync unchanged` only when you are
intentionally measuring a display-synchronized path.

For pre-PR validation from a source checkout, use the local wrapper. First check
that paths, scenario, thresholds, and generated commands are valid:

```bash
python scripts/benchmark/run_local_benchmark.py \
  --cache-dir /path/to/precompiled-cache \
  --dry-run
```

Then run the benchmark:

```bash
python scripts/benchmark/run_local_benchmark.py \
  --cache-dir /path/to/precompiled-cache \
  --label candidate-stack
```

The wrapper does not switch git branches. For a local baseline comparison, run
it from a separate `main` worktree/checkout first, then run it from the
candidate checkout with `--baseline-summary`:

```bash
python scripts/benchmark/run_local_benchmark.py \
  --cache-dir /path/to/precompiled-cache \
  --baseline-summary /path/to/main/benchmark-results/local/main/summary.json \
  --label candidate-stack
```

Compare two runs:

```bash
python scripts/benchmark/compare_benchmark_results.py \
  --baseline benchmark-results/baseline/summary.json \
  --candidate benchmark-results/current/summary.json \
  --output benchmark-results/comparison.json \
  --thresholds benchmarks/viewer-thresholds.v1.json
```

## Machine-local Devil's Eye XL run

The current local gold-map benchmark is hardwired to your machine and must not
be uploaded. The wrapper uses:

- source map: `~/Downloads/Maps/Devil's Eye XL`
- ignored local copy: `.benchmark-data/maps/devils-eye-xl`
- ignored local results/history: `.benchmark-data/results/devils-eye-xl`
- default route mode: vertex-footprint centerline route

First validate the plan:

```bash
python scripts/benchmark/run_devils_eye_xl_benchmark.py --dry-run
```

Then run it:

```bash
python scripts/benchmark/run_devils_eye_xl_benchmark.py
```

The first real run copies the map into `.benchmark-data` if needed. If the
local copy does not contain `_cache/manifest.json`, the wrapper compiles the
cache with:

```bash
python -m caveviewer.chunker --source .benchmark-data/maps/devils-eye-xl
```

The wrapper clears `CAVEVIEWER_MAP_CACHE_DIR` for the chunker and benchmark
subprocesses so the gold cache stays adjacent to the local map copy. Each run
tees wrapper, chunker, and benchmark-wrapper output into that run's
`orchestration.log`. Each successful benchmark also appends
`.benchmark-data/results/devils-eye-xl/history.jsonl` and writes
`.benchmark-data/results/devils-eye-xl/latest-summary.txt`. When a previous
local record exists, the new run is compared against it with
`benchmarks/viewer-thresholds.v1.json`; a regression returns a non-zero exit
code and still leaves all artifacts for inspection.

The console output and `latest-summary.txt` include the current metrics, the
single latest run used as the gate baseline, detailed threshold checks, and a
compact comparison against recent local history. Use `--history-limit <n>` to
change how many previous runs are listed.

By default the local wrapper generates an `auto-centerline-route-v1.json` file
inside the run artifact directory after the cache exists. It uses the
fine-grained `footprint_cells` manifest field, which the chunker derives from
source vertex positions, to estimate the middle of the cave passage. The
generator computes a high-clearance path through that footprint, where clearance
means grid distance from the footprint boundary, then selects a representative
segment instead of sweeping the entire cave. It estimates Y from the nearest
occupied chunk columns by placing the camera at a configurable vertical bias
inside the local min/max chunk bounds. The default bias is `0.65`, slightly
above the midpoint, so tall passages are more likely to stream and show ceiling
geometry. This better approximates what virtual divers tend to look at: a route
through the middle of the visible passage at a reasonable traversal rate.

This remains a deterministic benchmark route, not a collision-checked
navigation mesh. The cache describes surface geometry and chunk bounds; it does
not encode free water volume or diver-swimmable constraints.

Use these controls when calibrating the route:

```bash
python scripts/benchmark/run_devils_eye_xl_benchmark.py \
  --centerline-route-keyframes 10 \
  --centerline-route-target-length-m 1000 \
  --centerline-route-y-bias 0.65
```

To run the older dense-streaming-load proxy instead:

```bash
python scripts/benchmark/run_devils_eye_xl_benchmark.py \
  --route-mode auto-dense \
  --dense-route-keyframes 10 \
  --dense-route-percentile 85
```

To run the checked-in scenario exactly as written instead:

```bash
python scripts/benchmark/run_devils_eye_xl_benchmark.py --route-mode static
```

To make this run automatically before local pushes to `main`, install the
tracked pre-push hook template on this machine:

```bash
ln -sf ../../scripts/benchmark/hooks/pre-push-devils-eye-xl .git/hooks/pre-push
```

The hook only runs when the destination ref is `refs/heads/main`. It uses
`$PYTHON` when set, otherwise `.venv-dev/bin/python` when present, then falls
back to `python3`.

## Scenario file

Scenario files are versioned JSON. `benchmarks/gold-route-v1.json` is the
default route contract. It uses `position_mode: "first_chunk_center_offset"` so
the checked-in sample can run on any precompiled cache. Once the gold map is
finalized, replace the route with validated camera positions and use
`position_mode: "absolute"` if exact map coordinates are preferable.

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
printing setup guidance. This keeps the workflow available before the gold map
artifact is published without creating noisy CI failures.

## Calibration policy

Do not make first-run benchmark numbers release-blocking. Calibrate on the same
runner class with the same gold map:

1. Run the current release or `main` three or more times.
2. Inspect `summary.json`, `frames.jsonl`, and `benchmark.log` for warmup,
   shader/cache, or runner instability.
3. Set thresholds after observing normal variance.
4. Treat failures as release blockers only after the map artifact and runner
   class are stable.

Use the default comparison limits as starting values: 5% wall-clock FPS drop,
5% median render-FPS drop, 10% 1% low render-FPS drop, 15% p95 frame-time
increase, and 20% stutter-count increase.
