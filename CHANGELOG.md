# Changelog

All notable changes to this project are documented in this file.

## Release 1.0.77

- Map imports, video recording, and dive tracing now use clearer, more
  consistent progress messages, so it is easier to see what CaveViewer is
  doing.
- If you close CaveViewer while a video or dive trace is being saved, it now
  stays open until the file is finished, then closes automatically. This helps
  prevent a recording or trace from being lost when leaving the viewer.
- Removed unused internal compatibility and release-maintenance code to make
  the application easier to maintain.

## Release 1.0.76

- Fixed scrolling in the Map Library and Preferences for macOS trackpads and
  mice.
- The Map Library can now recognize many well-known caves and show useful
  details such as location and cave type. Choose **About cave** for a fuller
  description without changing how the map is downloaded, opened, or cached.
- The startup screen and Map Library are easier to use, with clearer
  navigation, more consistent text sizes, and room for map names that need two
  lines.
- Preferences and About now open in the main window instead of separate
  pop-up windows.
- Update messages are simpler: CaveViewer first confirms that an update is
  ready, then offers one clear link to show the downloaded update.

## Release 1.0.74

- Made the Your Recent Maps and CaveViewer Maps groups independently
  collapsible while keeping both open by default.
- Rebuild a map cache directly from Map Library without opening the map. The
  previous cache stays usable until its replacement succeeds, and background
  completion or failure can be reported through desktop notifications.
- Made first-time OBJ imports easier to recover: they can pause at a safe
  checkpoint and resume when the same map is opened again.
- Made very large map imports more dependable with protected background work,
  clearer progress, and earlier warnings when memory or disk space is too low.
- Reduced stutters and out-of-memory failures on lower-memory systems through
  smarter chunking, streaming, texture handling, and GPU memory budgeting.
- Polished the experience across Windows, macOS, and Linux with clearer
  startup and Map Library feedback, better DPI scaling, and safer signed
  updates.
