# Changelog

All notable changes to this project are documented in this file.

## Release 1.0.90

- The startup splash now remains visible for at least two seconds, avoiding a
  distracting flash on machines that initialize CaveViewer quickly.
- Update downloads keep the lower-left status area at two rows, with progress
  and cancellation presented together instead of shifting the layout.
- Update messages are shorter and no longer repeat whether the installed build
  uses the stable or preview channel.
- Older prerelease installations can once again discover current Preview
  updates without bringing the retired channel name back into the current UI
  or release tools.

## Release 1.0.83

- Windows now installs through a per-user setup program. Installed copies can
  download a verified update, choose **Install and restart**, and safely launch
  the new version; older ZIP installations keep the manual migration path.
- Updates are more dependable across platforms, with clearer status messages,
  cancellation, atomic package handling, and correct stable/preview channel
  behavior.
- Startup and Preferences are smoother on Windows: the splash screen stays
  responsive while Preferences initializes, and its layout no longer stalls.
- Simplified runtime settings and viewer-startup coordination, and removed
  obsolete navigation-certificate cache work to keep cache handling leaner.
- Strengthened Windows, macOS, and Linux package/release checks, including
  verified update metadata and on-demand macOS Intel smoke coverage.

## Release 1.0.78

- Added a new Help area in the main window with an easy-to-scan reference for
  keyboard shortcuts and capture features.
- You can now save an interesting section of a cave as its own CaveViewer map,
  ready to open independently or share with another CaveViewer user.
- Video recording, manual dive traces, and cave slices now use consistent
  keyboard shortcuts and clearer start/stop feedback.
- Refined the main-window layout with clearer navigation and more consistent
  tabs and scrolling in Help and Preferences.

## Release 1.0.77

- Map imports, video recording, and dive tracing now use clearer, more
  consistent progress messages, so it is easier to see what CaveViewer is
  doing.
- If you close CaveViewer while a video or dive trace is being saved, it now
  stays open until the file is finished, then closes automatically. This helps
  prevent a recording or trace from being lost when leaving the viewer.

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
