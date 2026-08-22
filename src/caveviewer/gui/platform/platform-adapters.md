# Platform-Specific Code

This package keeps platform-dependent behavior behind focused contracts. The
process-owned `PlatformRuntime` composes immutable profiles, feature gates,
desktop services, and small action adapters once at startup. Consumers receive
that runtime through dependency injection and do not perform their own platform
detection.

## Design rules

- Put static UI and release-policy choices in immutable profiles.
- Put process-stable capability decisions in `PlatformRuntime.feature_gates`.
- Probe mutable facts immediately before the action that needs them.
- Use the smallest focused adapter or service for native side effects.
- Keep `sys.platform`, native imports, shell commands, portals, and Tk dialogs
  inside this package.
- Treat optional actions such as notification or file reveal as best effort.

## Main components

- `runtime.py` composes the process-owned platform state without performing
  network, display, D-Bus, GPU, file-manager, or installer actions.
- `presentation.py` selects fonts, layouts, shortcuts, scaling, and input
  conventions. `presentation_actions.py` owns DPI setup, About-menu
  integration, and viewer focus.
- `desktop_services.py` owns file and directory selection, file/URI opening,
  notifications, and idle/suspend inhibition. Linux uses portal-first routes
  with conservative fallbacks.
- `update_package_storage.py`, `update_package_reveal.py`, and
  `update_package_install.py` separately own verified-package persistence,
  non-executing reveal, and signed Windows installer handoff.
- `saved_artifact_reveal.py`, `recording_process.py`, and `tls_trust.py` own
  artifact reveal, encoder process options, and native trust augmentation.
- `windowing.py` resolves Linux display plans; `window_backend.py` executes the
  selected GLFW/ModernGL launch target.
- `probes/` contains side-effect-free route and capability discovery used by
  pure policy in `caveviewer.gui.features`.

## Adding platform behavior

1. Classify the fact as process-stable, action-time, resource-policy input, or
   ordinary workflow error handling.
2. For static conventions, extend the appropriate immutable profile.
3. For native effects, add or extend a narrowly scoped protocol and direct
   platform implementation.
4. If availability affects product behavior, return a typed capability result
   from a side-effect-free probe and interpret it in pure feature policy.
5. Inject the focused dependency through `PlatformRuntime`; do not introduce a
   general-purpose platform facade.
6. Test policy tables, runtime composition, injected fakes, and the final
   action-boundary recheck where applicable.

## Update packages

`runtime.update_profile.install_channel` identifies the current distribution
channel. Storage adapters atomically promote verified packages into their
platform-specific locations. Reveal adapters expose packages without executing
them. Only the installer adapter may launch a package, and only after the
Windows EXE provenance, hash, and Authenticode checks pass following explicit
user consent.

## Saved artifacts and recording

Successful video and trace workflows use `SavedArtifactRevealAdapter` to show
the completed file. A reveal failure does not change the save result.
`RecordingProcessAdapter` supplies only native non-command `Popen` options;
encoder discovery and output-directory checks remain in the recording
preflight.

## TLS and desktop actions

`TlsTrustAdapter` augments a normally verifying SSL context at the update
network boundary. It never disables verification or decides update policy.
Notifications and idle inhibition use separate on-demand preflights and remain
optional. File and directory pickers have distinct typed routes so one action
cannot borrow another action's authority.

## Related files

- `src/caveviewer/gui/viewer_window.py`
- `src/caveviewer/gui/splash_screen.py`
- `src/caveviewer/gui/update_manager.py`
- `docs/development/architecture.md`
- `docs/development/releases.md`
