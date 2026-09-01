---
name: caveviewer-performance
description: "Run, compare, and interpret CaveViewer viewer performance benchmarks. Use for FPS regressions, benchmark scenarios, streaming performance, threshold calibration, render-loop profiling, or benchmark result analysis; not for functional import-lifecycle bugs or ordinary map opening."
---

# CaveViewer performance

Make performance conclusions from compatible, repeatable measurements rather
than a single run or subjective viewer feel.

## Define a comparable experiment

1. Read `docs/development/benchmarking.md` completely. Read
   `docs/development/rendering.md` for the affected streaming mechanism and
   `docs/development/architecture.md` before moving responsibilities.
2. Record the exact revision, platform, runner and hardware, Python and OpenGL
   environment, display/framebuffer size, preferences or environment overrides,
   cache manifest hash, scenario fingerprint, and threshold file.
3. Keep import compilation separate from FPS streaming. Direct viewer benchmarks
   start from a precompiled cache; use import tests for cache-build performance
   unless the task explicitly defines a separate import benchmark.

## Run and interpret

- Use `caveviewer-map-benchmark` for a private or local map; run `--dry-run`
  before measurement. Use `caveviewer-benchmark` for an existing cache and
  scenario, and `scripts/benchmark/compare_benchmark_results.py` for two saved
  summaries.
- Compare baseline and candidate from separate worktrees with the same map,
  cache, scenario, settings, actual window size, and runner class. Preserve all
  artifacts from failed comparisons.
- Treat `wall_clock_fps` as the primary release gate. Use render FPS, frame-time
  percentiles, stutter count, frame samples, environment data, and logs to
  diagnose CPU, GPU, I/O, scheduling, upload, or cache effects.
- Do not mix changed routes or cache identities into an old baseline. Keep route
  changes and threshold changes separately reviewable when practical.
- Do not lower a threshold based on one noisy run. Calibrate with at least three
  runs of the current release or `main` on the same stable runner class and map.

## Verify changes

Keep benchmark artifacts under ignored `benchmark-results/`, a map-local
`_benchmarks/`, or another explicit output directory; do not commit private
maps or generated results.

Run `tests/unit/test_benchmark_cli.py`,
`tests/unit/test_benchmark_compare_cli.py`, and
`tests/unit/test_benchmark_local_validation.py` for changed benchmark behavior,
plus the repository's standard validation. Run the real native benchmark when
the change affects timing or rendering, and report the baseline, candidate,
variance, thresholds, and hardware limitations.
