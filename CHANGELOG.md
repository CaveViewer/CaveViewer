# Changelog

All notable changes to this project are documented in this file.

## 1.2.3 - 2026-06-30

### Added
- Windows setup automation with `launch.bat` and `setup.ps1` for guided first-time installation (Python install, dependency install, Desktop shortcut creation).
- Windows update manifest and release distribution support.
- Development scripts organized in `scripts/dev/` for source-based installation and environment configuration.

### Changed
- Reorganized directory structure: moved `install.sh` and `env_setup.sh` to `scripts/dev/` to clearly indicate they are for development/source builds only.
- Improved in-app update functionality with more robust upgrade handling.
- Simplified Windows application packaging with integrated setup automation.
- Enhanced README documentation with step-by-step instructions for macOS, Windows, and source-based installations.
- Updated release process documentation in README.

### Fixed
- Removed update log file from distribution artifacts.

## 1.1 - 2026-06-27

### Added
- First stable release of the CaveViewer macOS app.

## 1.0.55 - 2026-06-27

### Added
- Configurable text anti-aliasing modes via `CAVEVIEWER_TEXT_AA_MODE` environment variable (normal, light, or lcd for Retina/high-DPI displays).

### Changed
- Increased text rendering supersampling (base grid height 7.0 → 8.5) for improved anti-aliasing quality.
- Updated run_caveviewer.sh with default environment variable values for performance tuning and text rendering.

## 1.0.54 - 2026-06-27

### Added
- Performance tuning environment variables: `CAVEVIEWER_CHUNK_BUILD_WORKERS` for cache-build chunk writer threads and `CAVEVIEWER_IO_WORKERS` for runtime chunk-load worker threads.
- TrueType/OpenType font support with antialiased glyph rendering for UI typeface (override with `CAVEVIEWER_UI_FONT` environment variable).
- Configurable text anti-aliasing modes via `CAVEVIEWER_TEXT_AA_MODE` (normal, light, or lcd for Retina displays).

### Changed
- UI typeface now uses proportional glyph advances with tighter default letter spacing (0.7) for cleaner, less blocky text.
- Increased text rendering supersampling (base grid height 7.0 → 8.5) for improved anti-aliasing quality.
- Enhanced UI style with light frosted neutrals and macOS-like blue accents across overlay widgets.

## 1.0.46 - 2026-06-27

### Changed
- Updated the macOS About dialog to use a more compact native layout.
- Reduced visual clutter by showing program name/version as primary text and credits as smaller secondary detail text.

## 1.0.45 - 2026-06-27

### Fixed
- Fixed a recurring crash when selecting About CaveViewer from the macOS File menu.
- Switched About handling to a native Tk messagebox path for more stable macOS menu callback behavior.

## 1.0.44 - 2026-06-27

### Changed
- Enforced DMG-only update flow on macOS by removing the legacy source-zip updater path.
- Locked release/runtime dependencies to pinned versions for reproducible builds.

### Security
- Added SHA-256 verification for downloaded update payloads before install handoff.
- Added SHA-256 verification in the detached updater before applying a DMG update.

## 1.0.43 - 2026-06-27

### Added
- Persistent camera bookmarks with save/recall hotkeys for slots 1-9.
- Bookmark shortcut documentation in the in-viewer help overlay.
- Bookmark shortcut documentation in the README controls table.

### Changed
- Improved macOS bookmark shortcut handling so Command+number save is detected more reliably across window backends.
- Added a macOS fallback save shortcut (Shift+1..9) when Command modifier reporting is inconsistent.

### Fixed
- Fixed a crash when opening About CaveViewerMac from the macOS File menu.
- Hardened About callback handling for Tk/macOS command invocation edge cases.
