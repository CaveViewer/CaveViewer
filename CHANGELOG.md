# Changelog

All notable changes to this project are documented in this file.

## Release 1.0.67

- Added resumable first-time OBJ imports. Active imports can now be paused at a
  safe checkpoint and resumed automatically when the same map is reopened.
- Improved import robustness for large maps with spawned import processes,
  heartbeat reporting, staged texture assets, earlier RAM/disk safety checks,
  cancelled-import cleanup, and better parent-process error reporting.
- Reworked chunking, streaming, and rendering behavior for lower-memory systems.
  Worker limits now grow conservatively from advisory caps, chunk builds are more
  memory-aware, and nearby geometry is preserved under GPU pressure by
  downscaling oversized textures instead of dropping chunks.
- Expanded GPU and system-memory budgeting across platforms, including NVIDIA
  `nvidia-smi` detection, Linux AMD DRM sysfs detection, integrated/shared-memory
  handling, safer fallback budgets, and clearer manual GPU memory ceiling
  semantics.
- Refined the viewer and startup experience across Windows, macOS, and Linux:
  advanced settings, splash sizing, DPI scaling, sample-map dialogs, progress
  indicators, control-panel rendering, profiI see map chunker evolve into an intelligent module where people don't have to think aboutI see map chunker evolve into an intelligent module where people don't have to think about chunk size (unle chunk size (unlele viewing, bookmarks, minimap
  behavior, FPS/readout handling, and movie recording all received updates.
- Strengthened update and release infrastructure with signed updater manifests,
  stricter public-key signature checks, platform-specific release workflows,
  parallel all-platform packaging, and improved Linux/macOS/Windows build
  scripts.
- Reorganized the application into the `src/caveviewer` package layout and
  refreshed documentation, developer notes, website content, screenshots,
  desktop metadata, and test coverage.