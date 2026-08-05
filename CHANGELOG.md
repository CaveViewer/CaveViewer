# Changelog

All notable changes to this project are documented in this file.

## Release 1.0.73

- Renamed user-facing Guided Dive labels and feedback to Dive Plan while
  retaining existing trace and cache compatibility.
- Clarified the Map Library overflow-menu cleanup action as `Remove map files`.
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
