# Platform-Specific Code Module

This directory implements focused platform boundaries that provide macOS,
Windows, Linux, and fallback behavior without scattering conditional logic
throughout the codebase. `SplashPlatformAdapter` is a shrinking compatibility
surface for concerns that have not yet moved behind a focused contract.

## Directory Structure

```
platform/
├── __init__.py              # Public API exports
├── app_identity.py          # Native window identity and Tk root options
├── base.py                  # SplashPlatformAdapter protocol definition
├── desktop_services.py      # Desktop file, URI, notification, and inhibit services
├── desktop_inhibition.py    # Optional inhibitor authorization/acquisition
├── desktop_notifications.py # Optional notification authorization/execution
├── directory_selection.py   # Shared action-time directory-picker authorization
├── file_selection.py        # Shared action-time file-picker authorization
├── presentation.py          # Immutable fonts, layouts, shortcut, and input profile
├── presentation_actions.py  # Direct native DPI, About-menu, and focus actions
├── runtime.py               # Per-process platform composition and feature gates
├── update_package_reveal.py # Focused non-executing verified-package facade
├── update_package_storage.py # Focused verified-package storage facade
├── update_package_install.py # Focused signed Windows EXE handoff facade
├── windows_update_paths.py   # User-owned Windows update/log locations
├── saved_artifact_reveal.py # Focused post-save user-artifact reveal facade
├── saved_recording_reveal.py # Compatibility imports for former recording facade
├── recording_process.py      # Focused recording-encoder startup facade
├── tls_trust.py               # Focused native TLS-trust augmentation facade
├── probes/desktop.py        # On-demand desktop action route declarations
├── probes/update_package_reveal.py # Static package-reveal route declaration
├── probes/updates.py        # Static signed-update configuration and target probe
├── probes/recording.py      # On-demand encoder and output-directory preflight
├── probes/windowing.py      # On-demand side-effect-free viewer launch probe
├── portal.py                # Linux XDG Desktop Portal transport and states
├── windowing.py             # Pure Linux GLFW Wayland/X11 plan resolver
├── window_backend.py        # Typed native GLFW/ModernGL launch executor
├── factory.py               # Platform detection and adapter instantiation
├── macos.py                 # macOS-specific implementations
├── windows.py               # Windows-specific implementations
├── linux.py                 # Linux-specific implementations
└── default.py               # Default/fallback implementations for non-macOS platforms
```

## Architecture Overview

Focused protocols and immutable profiles keep platform behavior local while
still allowing deterministic test injection. The broad
`SplashPlatformAdapter` protocol remains only for compatibility concerns that
have not yet received a focused owner; new features must not add methods to it.

`SplashPlatformAdapter` continues to own unmigrated package behavior. Static
GUI presentation now lives in the immutable `PresentationProfile`, selected
purely from the composed platform name. It contains fonts, Tk and splash
layouts, shortcut/input conventions, text scaling, and startup sizing choices;
it never creates a Tk root, probes a display, or invokes native APIs.
`PresentationActionsAdapter` owns the small action-time native boundary for
process DPI setup, macOS About registration, and viewer focus. Its direct
Windows, macOS, and fallback implementations are selected from the composed
platform name without constructing Tk objects or invoking native APIs.

Static update release policy now lives in the immutable
`UpdateProfile` selected from platform and process-architecture facts; the
runtime checker and downloader receive its resulting `UpdateTarget`, not the
broad adapter. `UpdateManager` requires the process-owned `PlatformRuntime`
that contains this target and focused TLS adapter. `DesktopServices` is
intentionally separate: splash, viewer, settings, map-library, and
background-task code request host-desktop behavior through one capability
instead of importing Tk, shell commands, or D-Bus directly. Linux implements
file/directory selection, file/URI opening, file reveal, notifications, and
idle/suspend inhibition portal-first, with conservative fallbacks for
non-portal sessions. Long map library downloads use notification and inhibit
requests through `DesktopServices`, suppressing duplicate desktop notifications
while the Map Library panel owns foreground feedback; background update
downloads use notification and inhibit requests while the package is downloaded
and verified; uncached map imports use inhibit requests while parsing and
building the cache. These requests are best-effort and must not affect the
underlying operation.

`windowing.py` owns the pure Linux display-protocol plan resolver. Automatic
mode selects X11/XWayland before Wayland when both session endpoints exist so
source, debugger, and AppImage launches use the same GNOME window-management
path. `window_backend.py` owns native execution of an already-authorized
target: GLFW loading, native hints, EGL context setup, work-area sizing, retry,
and cleanup. It retries only a recognized initialization/window-creation
failure; renderer and application exceptions propagate without opening a
second backend.

`app_identity.py` owns native-window identity. Tk roots use the stable Linux
desktop application ID as their class name, matching the desktop file,
AppStream metadata, icons, and GLFW Wayland/X11 hints so GNOME can group and
activate CaveViewer windows consistently.

The Linux desktop file keeps `Exec ... %f` and MIME registrations for
`model/gltf-binary` and `model/obj`, so file managers can offer CaveViewer for
direct `.glb` and `.obj` launches. The application startup path must continue
accepting both folders and those direct files.

## Runtime composition and feature gates

`caveviewer.app` creates one `PlatformRuntime` after command-line overrides
have been applied for every interactive viewer path, including a direct CLI map
launch. It owns a stable `PlatformProfile`, `PresentationProfile`,
`PresentationActionsAdapter`, typed `UpdateProfile`, resolved
`UpdateConfiguration`, one broad compatibility adapter, one `DesktopServices`
instance, immutable capability results, and feature-gate decisions. It is not a
global singleton: callers receive it by injection, which keeps test setup
deterministic and prevents unrelated GUI surfaces from repeatedly constructing
portal-backed services.

`feature_gates` is deliberately limited to process-stable decisions. An update
target and the OS-selected update-package reveal route are safe to evaluate
once at composition time, so they appear in that registry. A mutable action
prerequisite is different: an on-demand probe returns a fresh
`CapabilityResult`, its pure policy returns a `FeatureDecision`, and the
feature service uses that paired preflight result only for the current action.
`UNKNOWN` is interpreted by the feature policy; it is not silently converted
into either enabled or disabled behavior.

The first migrated feature is automatic update checking and downloading:

```text
automatic update
    -> static UpdateProfile from platform and architecture facts
    -> UpdateConfiguration after explicit overrides
    -> immutable UpdateTarget capability result
    -> pure FeatureDecision policy and focused TLS adapter
    -> checker/download network work
```

The update profile is a pure transform, while the configuration is resolved
when the runtime is composed rather than when `update_checker.py` is imported.
This means CLI `--update-branch` and other explicit configuration are seen by
the process-owned manager. `UpdateTarget` carries the signed manifest URLs,
user agent, accepted package policy, and manifest aliases needed by the network
client, so the normal runtime path calls the typed target checker/downloader
instead of broad adapter update methods. `UpdateManager` receives that composed
runtime before it reads a gate, rather than creating update policy itself. The
manager rechecks the gate before it starts a check
and before it starts a download; a disabled gate never starts network work.
Offline failures remain ordinary transient check results, not a platform
capability failure.

Verified update-package reveal is a separate static gate and focused adapter:

```text
verified update package reveal
    -> pure FeatureDecision policy
    -> immutable Finder / Explorer / desktop-service route declaration
    -> UpdatePackageRevealAdapter
    -> non-executing native reveal
```

The route declaration has no native side effects. `UpdateManager` checks the
decision again before revealing a verified payload, and the splash omits the
reveal action when it is disabled. Direct focused adapters preserve macOS
read-only DMG mounting, Windows Explorer selection, and the Linux
desktop-service fallback without depending on the broad splash adapter.

Verified package persistence has its own focused
`UpdatePackageStorageAdapter`. It is invoked only after checksum verification,
so it has no process-static capability probe or feature gate: a user-visible
storage location can become unavailable while the app is running. Its direct
adapters preserve established naming, collision, macOS DMG, and Linux
AppImage behavior. A persistence exception is an ordinary update-workflow
failure, after which `UpdateManager` performs its normal temporary-file
cleanup.

The separately composed `UpdatePackageInstallerAdapter` is intentionally not a
broad update adapter or a feature gate. Its default implementation is a
fail-closed no-op everywhere except a registered frozen Windows installer
payload. Only a manifest-bound EXE with a nonempty Authenticode publisher can
request its label. At the explicit action boundary it rechecks the private
payload's size/SHA-256 and Windows signature, then starts the Inno installer
with a distinct argument vector. Source/ZIP Windows launches keep the existing
manual reveal route.

Saved-artifact reveal is a focused post-save `SavedArtifactRevealAdapter`. It
runs only after a video encoder or trace writer reports success for a
user-visible stop, so it does not require a capability probe or feature gate:
a file-manager failure cannot undo an artifact that was already saved. Its
compatibility facade delegates to the broad adapter's established Finder,
Explorer, and Linux desktop-service behavior. The viewer logs a reveal failure
while preserving the successful artifact status.

Recording encoder startup has its own `RecordingProcessAdapter`. It runs only
after the existing on-demand video-recording preflight and provides the
platform-specific non-command `Popen` kwargs used to launch ffmpeg. It neither
selects the encoder nor decides recording availability. Its compatibility
facade preserves Windows `STARTUPINFO`/`CREATE_NO_WINDOW` console suppression
and the empty default, macOS, and Linux option sets. Desktop notifications and
idle/suspend inhibition are independent optional action-time migrations.

TLS trust augmentation has its own `TlsTrustAdapter`. It augments a fresh,
normally verifying SSL context with native trust roots immediately before
update networking. Its compatibility facade preserves Windows `CA` and `ROOT`
certificate-store loading and the empty default, macOS, and Linux behavior.
It does not decide whether updates are available, disable certificate
verification, or alter the separate process-global `truststore` startup
compatibility path.

Video recording is the first on-demand gate. When the user starts recording,
the viewer asks the runtime for a `VideoRecordingPreflight`: one narrow probe
for an ffmpeg path and writable output directory paired with the pure
video-recording decision from that exact snapshot. The same preflight is
requested again immediately before ffmpeg starts, so a changed drive or folder
permission cannot begin a recording that has nowhere reliable to go. No encoder
lookup or output-directory write check happens during application startup.

Directory selection is another on-demand gate. A
`DirectorySelectionPreflight` records the desktop service's declared route for
one picker action: Linux is `portal_then_tk`, a portable Tk service is the
degraded `tk` route, and a legacy injected service is the degraded `injected`
route. Declaring a route does not create a Tk root or contact D-Bus. Splash,
viewer, and Preferences browse actions request a fresh preflight immediately
before opening a chooser; `LinuxPortalDesktopServices` retains its action-time
fallback when a portal request fails. The Preferences “Downloaded maps folder”
control is Map Library's directory-setting surface, so it shares this same
authorization rather than creating a Map Library-specific gate.

File opening has the corresponding `FileSelectionPreflight`: its separately
typed route prevents a Guided Dive file picker from borrowing directory-picker
authority. The action boundary rechecks the declaration immediately before
`choose_file`, while Linux keeps its existing Portal-to-Tk fallback internally.

Desktop notifications are an optional on-demand action, not a gate on update
or map-library work. `DesktopNotificationPreflight` pairs a side-effect-free
route declaration with pure policy: Linux declares `portal_then_noop`, portable
Tk declares an unavailable no-op route, and legacy injected services remain a
degraded route. `desktop_notifications.py` rechecks that typed target before
each send or withdrawal, then converts unavailable, unknown, changed, and
native-action failures into diagnostic no-ops. It does not contact D-Bus during
the probe, and `PlatformRuntime.feature_gates` does not cache the result.

Idle/suspend inhibition is likewise optional, but it owns a scoped resource.
`IdleSuspendInhibitionPreflight` uses a separate typed target: Linux declares
`portal_then_noop`, portable Tk declares an unavailable no-op route, and legacy
injected services remain a degraded route. `desktop_inhibition.py` rechecks the
target immediately before acquisition and returns no handle on an unavailable,
unknown, changed, or failed route. Releasing a handle that was actually
acquired is ordinary best-effort cleanup rather than another preflight, so a
desktop-state change cannot leak it. The probe neither opens D-Bus nor starts a
Portal inhibition worker, and this mutable result is not cached in
`PlatformRuntime.feature_gates`.

Viewer launch is an on-demand capability rather than a startup gate.
`ViewerLaunchPreflight` pairs a typed native/GLFW target with pure policy from
current display/session and requested-backend facts. Its probe never imports or
initializes GLFW, creates a test window, or allocates a rendering context. The
viewer rechecks the target immediately before `WindowBackendAdapter` executes
it. The adapter receives the exact selected X11/Wayland plan and owns only the
current GLFW/ModernGL native work, including the constrained automatic retry;
it does not decide policy or retry renderer/application failures. A future
macOS Metal launch route can use the same target/adapter seam without changing
map-opening callers.

`SplashPlatformAdapter` remains a compatibility surface for unmigrated
platform actions. `PresentationProfile` owns static GUI choices and
`PresentationActionsAdapter` owns the three native presentation actions
directly; neither its factory nor its action implementations depend on the
broad adapter.
Automatic-update policy and manifest parsing have moved to `UpdateProfile` and
`UpdateTarget`; adapter-based update calls are likewise local compatibility
paths. `UpdatePackageRevealAdapter`, `UpdatePackageStorageAdapter`, and the
narrowly scoped `UpdatePackageInstallerAdapter` own direct package actions.
`SavedArtifactRevealAdapter`, `RecordingProcessAdapter`,
and `TlsTrustAdapter` remain narrow facades around existing artifact, recording,
and network actions. New features should add the smallest appropriate
combination of a probe, a pure policy in
`caveviewer.gui.features`, and an injected action adapter rather than expanding
this broad protocol. Cache, chunk streaming, navigation, and map state are
outside this runtime layer.

## Key Components

### `base.py` – Protocol Definition

Defines `SplashPlatformAdapter`, the frozen compatibility Protocol for
unmigrated action-time platform effects:

```python
class SplashPlatformAdapter(Protocol):
    def reveal_file(self, path: str) -> None:
        ...

    def load_system_certificates(self, context: object) -> None:
        ...

    def recording_subprocess_startup_kwargs(self) -> dict:
        ...
```

Fonts, layouts, shortcut/input conventions, text scaling, startup focus policy,
and viewer sizing do not belong to this protocol. UI code reads
`PlatformRuntime.presentation_profile` (or a pure direct fallback) instead.
Native presentation effects use `PresentationActionsAdapter`.

**When to modify**: Only for an existing compatibility concern that has not
yet been split. New platform-dependent features use a narrow edge probe, pure
policy, and injected action adapter or service instead of expanding this broad
protocol.

### `presentation.py` and `presentation_actions.py` – UI conventions and effects

`select_presentation_profile(platform_name=...)` is a pure table selection. A
`PresentationProfile` is frozen and process-stable: it holds only user-visible
conventions such as `splash_layout`, `ui_font_family`, key labels, mouse-look
button, and scaling rules. Linux fontconfig resolution is intentionally an
action-time fallback in `font_candidates_for_profile()`, not a selection-time
probe.

`PresentationActionsAdapter` has only `configure_process_dpi_awareness()`,
`install_about_handler()`, and `focus_viewer_window()`. Those calls occur at
their native action boundaries, after consumers have already selected static
presentation data from the profile. Its factory selects direct Windows, macOS,
or conservative fallback implementations from the composed platform name; the
factory has no Tk, display, or native-action side effects.

### `factory.py` – Platform Detection

```python
def get_platform_adapter() -> SplashPlatformAdapter:
    if sys.platform == "darwin":
        return MacOSSplashPlatformAdapter()
    if sys.platform.startswith("win"):
        return WindowsSplashPlatformAdapter()
    if sys.platform.startswith("linux"):
        return LinuxSplashPlatformAdapter()
    return DefaultSplashPlatformAdapter()
```

**How it works**:
- Inspects `sys.platform` by default, or an injected composition-time platform
  fact in tests
- Returns the appropriate adapter instance
- Can share an injected `DesktopServices` object with the Linux adapter
- Falls back to `DefaultSplashPlatformAdapter` for unknown platforms

**When to modify**: Only if adding support for a new platform that requires different `sys.platform` detection (rare).

### `runtime.py` – Process-owned platform state

`create_platform_runtime()` is the application composition boundary. It reads
the selected environment once, after app-level command-line overrides, and
creates immutable presentation, update capability, and feature-decision values.
It must not perform network checks, D-Bus calls, GPU probes, file-manager
launches, DMG mounts, or other expensive on-demand work while it is being
composed.

Inject `PlatformRuntime` into a feature service when migrating it. The service
must use the runtime's focused adapter and `DesktopServices`, rather than construct a
second one. Existing callers of `get_platform_adapter()` and
`get_desktop_services()` remain valid only as compatibility paths until their
concerns are split out of `SplashPlatformAdapter`.

### `macos.py` – macOS Implementations

`MacOSSplashPlatformAdapter` retains Finder reveal. Its fonts, layouts, input
conventions, and scaling are selected by the macOS `PresentationProfile`;
its About-menu registration and viewer focus are direct
`MacOSPresentationActionsAdapter` effects.

### `windows.py` & `linux.py` – Platform Overrides

`WindowsSplashPlatformAdapter` retains certificate-store loading,
recording-process startup flags, and Explorer reveal.
`LinuxSplashPlatformAdapter` retains portal-backed file reveal. Their static
presentation values are selected by `PresentationProfile`, not overridden on
the broad adapter. Windows DPI setup belongs to
`WindowsPresentationActionsAdapter`.

### `default.py` – Fallback Implementations

Provides conservative fallbacks for remaining compatibility actions:

```python
class DefaultSplashPlatformAdapter(SplashPlatformAdapter):
    def reveal_file(self, path: str) -> None:
        raise RuntimeError("Revealing files is unsupported on this platform")
```

## Usage Examples

### In application code (e.g., `viewer_window.py`):

```python
from caveviewer.gui.platform.runtime import PlatformRuntime

def configure_viewer(runtime: PlatformRuntime) -> None:
    profile = runtime.presentation_profile

    # Static presentation comes from the immutable process-owned profile.
    if profile.mouse_look_button_name == "right":
        # macOS: show "Right click + mouse"
        pass

    modifier = profile.bookmark_save_modifier  # "command" or "control"
    font = profile.ui_font_family  # Platform-specific UI font
```

No feature code needs `if sys.platform == ...` checks or its own platform
factory call.

## How to Add Platform-Specific Functionality

1. Classify the work before adding a gate: process-stable capability,
   on-demand action preflight, resource-policy input, or ordinary workflow
   error handling. Do not turn every behavior into a feature gate.
2. For a new platform-dependent feature, add a narrow edge probe that returns
   `CapabilityResult`, a side-effect-free policy in `caveviewer.gui.features`,
   and a focused injected action adapter or service. The policy returns a
   `FeatureDecision` with a stable reason code, user-safe explanation, and
   route.
3. Put only process-stable decisions in `PlatformRuntime.feature_gates`.
   Action-time facts such as a selected path, portal availability, or an
   encoder executable remain on-demand and are checked again before execution.
4. For static UI conventions, add a field or pure transform to
   `PresentationProfile`; for native UI effects, add the smallest focused
   action method. Use `SplashPlatformAdapter` only when migrating an existing
   compatibility method. Do not add unrelated new methods to it.
5. Test four boundaries: pure policy table, fake probe/adapter, runtime
   composition and injection, and consumer enforcement/recheck.

## Example: Keyboard Shortcuts

### Problem
macOS uses Cmd+1..9 to save bookmarks; Windows/Linux use Ctrl+1..9.

### Solution

1. **Selected in the profile** (`presentation.py`):
   ```python
   profile = select_presentation_profile(platform_name="darwin")
   assert profile.bookmark_save_modifier == "command"
   ```

2. **Selected per platform** (`presentation.py`):
   - macOS: Returns `"command"`
   - Windows/Linux: Returns `"control"`

3. **Used in UI code** (`controls_overlay.py`):
   ```python
   profile = runtime.presentation_profile
   if profile.bookmark_save_modifier == "command":
       rows.append(("Cmd + 1..9", "Save camera bookmark slot"))
   else:
       rows.append(("Ctrl + 1..9", "Save camera bookmark slot"))
   ```

## Testing Platform-Specific Code

For unit testing with mocked platforms:

```python
from typing import Protocol

profile = select_presentation_profile(platform_name="darwin")
assert profile.bookmark_save_modifier == "command"
assert profile.mouse_look_button_name == "right"

# Inject mock in tests, then verify behavior
```

## Common Patterns

### UI Strings with Platform Differences
**When**: User-facing text that varies by platform (e.g., "Cmd+1" vs "Ctrl+1")

**How**:
1. Query the injected runtime profile for the variable part (for example,
   `bookmark_save_modifier`).
2. Build the full string in UI code without adding platform checks.

### Update Packages & Distribution Channels
**When**: Different distribution formats (DMG for macOS, signed EXE or legacy ZIP for Windows, AppImage for Linux)

**How**:
- `runtime.update_profile.install_channel` identifies the current package channel
- `UpdatePackageStorageAdapter.persist_verified_package()` promotes a verified
  temporary package into platform-specific user-visible storage. Its direct
  platform adapters preserve Downloads naming, collision suffixes, macOS DMG
  fallback naming, and Linux AppImage permissions. Windows keeps eligible EXE
  installers in its private LocalAppData update root; ZIP migration payloads
  remain in Downloads. They copy through a hidden sibling and atomically
  publish only the completed package.
- `UpdatePackageRevealAdapter.reveal_action_label()` exposes the platform-native
  reveal label through the focused boundary. `UpdateManager` copies that static
  value into its immutable snapshot, and the compact splash renders it after
  the one-label ready-state delay without consulting the broad adapter.
- `UpdatePackageRevealAdapter.reveal_verified_package()` exposes the verified
  package without running it
- The typed update profile knows which channel the current build came from

Revealing is deliberately manual and non-executing. macOS mounts the DMG
read-only and reveals its `.app` in Finder, Windows selects the package in
Explorer, and Linux uses `OpenURI.OpenDirectory` with `xdg-open` fallback.
Only `UpdatePackageInstallerAdapter` may launch an installer, and only after
the Windows EXE provenance, rehash, and Authenticode checks pass after explicit
user consent. Reveal adapters never execute a downloaded package.

### Saved Files
**When**: A workflow creates a user-owned output file, such as an MP4 video or
Guided Dive JSONL trace, and should show the file in the native file manager
after a user-visible completion.

**How**:
- `SavedArtifactRevealAdapter.reveal_saved_artifact()` exposes a completed
  artifact without opening or executing it.
- `reveal_file()` remains the compatibility implementation until artifact
  reveal behavior moves behind that focused adapter.
- macOS reveals the file in Finder.
- Windows selects the file in Explorer.
- Linux uses desktop-service reveal, with portal support and fallback behavior
  centralized outside viewer code.

Callers should treat reveal as best-effort and keep the primary success state
intact if the file manager cannot be launched.

### Recording Encoder Process

**When**: A video-recording preflight has already approved an ffmpeg target and
the viewer is about to start its encoder session.

**How**:
- `RecordingProcessAdapter.encoder_popen_kwargs()` supplies only native
  non-command `Popen` options.
- `recording_subprocess_startup_kwargs()` remains the compatibility
  implementation until process-startup behavior moves behind that focused
  adapter.
- Windows continues to suppress the GUI-launched ffmpeg console; default,
  macOS, and Linux behavior remains empty.

The adapter must not replace the on-demand recording preflight, select an
encoder binary, build the ffmpeg command, or own encoder worker lifecycle.

### TLS Trust

**When**: A network client is about to use a fresh Python SSL context for an
update manifest, signature, or payload request.

**How**:
- `TlsTrustAdapter.augment_ssl_context()` adds native trust roots while the
  default SSL verification policy remains enabled.
- `load_system_certificates()` remains the compatibility implementation until
  native certificate-store behavior moves behind the focused adapter.
- Windows continues to load the `CA` and `ROOT` stores; default, macOS, and
  Linux behavior remains empty.

The adapter does not create a feature gate, decide network availability, bypass
certificate verification, or replace the separate process-global `truststore`
startup compatibility path.

### UI Framework & Fonts
**When**: Native UI elements (menus, fonts, dialogs) behave differently per-platform

**How**:
- `PresentationProfile.ui_font_family` supplies the platform-appropriate font
- `PresentationActionsAdapter.install_about_handler()` integrates with the
  native About menu on macOS

## Best Practices

1. **Centralize, don't scatter**: Use injected runtime adapters/services for
   migrated features; never add platform checks directly in feature code.
2. **Default to safe**: The `DefaultSplashPlatformAdapter` should provide the most conservative, widely-compatible behavior
3. **Document why**: When platform implementations differ, document the reason in comments
4. **Test both paths**: Verify behavior on at least macOS and Windows/Linux if possible
5. **Keep it simple**: Keep compatibility protocol methods focused and split
   new native actions into narrow adapters rather than creating a god-adapter.

## Related Files

- **`src/caveviewer/gui/controls_overlay.py`**: Uses `PresentationProfile` to display platform-specific controls
- **`src/caveviewer/gui/viewer_window.py`**: Uses the profile for keyboard/mouse conventions and the action facade for native focus
- **`src/caveviewer/gui/update_manager.py`**: Owns update state, persistence, and package reveal
- **`src/caveviewer/gui/splash_screen.py`**: Presents update state and platform action labels
- **`docs/development/releases.md`**: Defines platform release artifacts, update manifest paths, and signing behavior
