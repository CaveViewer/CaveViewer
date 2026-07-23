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

Compare two runs:

```bash
python scripts/benchmark/compare_benchmark_results.py \
  --baseline benchmark-results/baseline/summary.json \
  --candidate benchmark-results/current/summary.json \
  --output benchmark-results/comparison.json \
  --thresholds benchmarks/viewer-thresholds.v1.json
```

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

Use the default comparison limits as starting values: 5% median FPS drop, 10%
1% low FPS drop, 15% p95 frame-time increase, and 20% stutter-count increase.
