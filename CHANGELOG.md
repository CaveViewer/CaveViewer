# Changelog

All notable changes to this project are documented in this file.

## Recent Changes

### Added
- Advanced startup settings for streaming and map parsing.
- Pre-compute and runtime streaming tuning options, including memory and worker-thread controls.
- Map parsing preferences with cache compatibility handling when preferences differ from existing manifests.
- Logging framework and expanded runtime diagnostics.
- Windows setup automation improvements and packaging/release support updates.

### Changed
- Splash/startup UI layout and sizing refined across macOS, Windows, and Linux, including DPI scaling updates.
- Startup and viewer visual consistency improved, including progress bar behavior and control panel styling.
- Build and release scripts streamlined, with Linux Docker-based multi-arch build flow updates.
- Documentation refreshed for platform support, source usage, and setup guidance.
- Runtime defaults and environment-based performance tuning expanded (GPU/memory/worker settings).

### Fixed
- macOS About menu crash and related callback stability issues.
- Linux compatibility issues (including Ubuntu/Fedora adjustments and font path handling).
- Windows loading/setup and progress behavior issues.
- Texture filename parsing edge cases in MTL handling.
- Open dialog behavior for pre-compiled map binaries.
- Startup dialog/advanced settings interaction issues, spacing regressions, and accidental-click exceptions.
