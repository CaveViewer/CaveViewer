# Changelog

All notable changes to this project are documented in this file.

## Recent Changes

### Added
- Advanced startup settings for streaming and map parsing.
- Pre-compute and runtime streaming tuning options, including memory and worker-thread controls.
- Automatic GPU memory budgeting: NVIDIA detection through `nvidia-smi`, Linux
  AMD detection through DRM sysfs, integrated AMD shared-memory allowance, and a
  Windows fallback budget for AMD/Intel systems without GPU-memory detection.
- Map parsing preferences with cache compatibility handling when preferences differ from existing manifests.
- Logging framework and expanded runtime diagnostics.
- Windows setup automation improvements and packaging/release support updates.

### Changed
- All-platform releases now package targets in parallel from one source revision and publish through a single signed-metadata finalizer.
- Removed the longitudinal cross-section map and its auxiliary import cache to reduce map-open and import latency.
- Advanced Settings now shows numeric defaults initially and muted in-field range placeholders when values are cleared.
- Splash/startup UI layout and sizing refined across macOS, Windows, and Linux, including DPI scaling updates.
- Startup and viewer visual consistency improved, including progress bar behavior and control panel styling.
- Build and release scripts streamlined, with Linux Docker-based multi-arch build flow updates.
- Documentation refreshed for platform support, source usage, and setup guidance.
- Runtime defaults and environment-based performance tuning expanded (GPU/memory/worker settings).
- Streaming and cache-building worker limits are now treated as advisory caps:
  both pools start conservatively and grow only while current system RAM
  availability allows it.
- Generated map caches are managed under the platform cache root by default,
  are self-contained with staged texture assets, and no longer use adjacent
  `_cache` or `.caveviewer_cache` discovery.

### Fixed
- Large textured maps now keep geometry visible under GPU pressure by
  downscaling oversized textures instead of dropping nearby chunks from the
  streaming set.
- Texture downscaling diagnostics now log the GPU budget, target percentage,
  texture share, unique texture count, selected texture cap, and first actual
  resize.
- Windows stable and prerelease publishing now signs updater manifests and
  commits the required `.json.sig` files.
- macOS About menu crash and related callback stability issues.
- Linux compatibility issues (including Ubuntu/Fedora adjustments and font path handling).
- Windows loading/setup and progress behavior issues.
- Texture filename parsing edge cases in MTL handling.
- Open dialog behavior for pre-compiled map binaries.
- Startup dialog/advanced settings interaction issues, spacing regressions, and accidental-click exceptions.
