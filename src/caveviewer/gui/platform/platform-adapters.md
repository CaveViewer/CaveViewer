# Platform-Specific Code Module

This directory implements a **platform adapter pattern** that allows the CaveViewer application to provide platform-specific behavior across macOS, Windows, and Linux without scattering conditional logic throughout the codebase.

## Directory Structure

```
platform/
├── __init__.py              # Public API exports
├── app_identity.py          # Native window identity and Tk root options
├── base.py                  # SplashPlatformAdapter protocol definition
├── desktop_services.py      # Desktop file, URI, notification, and inhibit services
├── directory_selection.py   # Shared action-time directory-picker authorization
├── runtime.py               # Per-process platform composition and feature gates
├── update_package_reveal.py # Focused non-executing verified-package facade
├── update_package_storage.py # Focused verified-package storage facade
├── saved_recording_reveal.py # Focused post-save recording reveal facade
├── probes/desktop.py        # On-demand directory-selection route declaration
├── probes/update_package_reveal.py # Static package-reveal route declaration
├── probes/updates.py        # Static signed-update configuration and target probe
├── probes/recording.py      # On-demand encoder and output-directory preflight
├── portal.py                # Linux XDG Desktop Portal transport and states
├── windowing.py             # Linux GLFW Wayland/X11 selection and fallback
├── factory.py               # Platform detection and adapter instantiation
├── macos.py                 # macOS-specific implementations
├── windows.py               # Windows-specific implementations
├── linux.py                 # Linux-specific implementations
└── default.py               # Default/fallback implementations for non-macOS platforms
```

## Architecture Overview

The module uses Python's **Protocol** pattern to define a contract (`SplashPlatformAdapter`) that each platform implementation must satisfy. This allows:

- **No scattered runtime conditionals**: One factory selects the adapter, and feature code calls adapter methods
- **Easy testing**: Mock adapters can be injected for testing specific platforms
- **Extensibility**: New platform-specific methods can be added to the protocol and implemented per-platform
- **Maintainability**: Platform-specific logic is isolated in dedicated files

`SplashPlatformAdapter` continues to own update-channel, control, font, and
package behavior. `DesktopServices` is intentionally separate: splash, viewer,
settings, map-library, and background-task code request host-desktop behavior
through one capability instead of importing Tk, shell commands, or D-Bus
directly. Linux implements file/directory selection, file/URI opening, file
reveal, notifications, and idle/suspend inhibition portal-first, with
conservative fallbacks for non-portal sessions. Long map library downloads use
notification and inhibit requests through `DesktopServices`, suppressing
duplicate desktop notifications while the Map Library dialog owns foreground
feedback; background update downloads use notification and inhibit requests
while the package is downloaded and verified; uncached map imports use inhibit
requests while parsing and building the cache. These requests are best-effort
and must not affect the underlying operation.

`windowing.py` owns Linux display-protocol selection. Automatic mode attempts
X11/XWayland before Wayland when both session endpoints exist so source,
debugger, and AppImage launches use the same GNOME window-management path. It
retries only an initialization/window-creation failure. Renderer and application
exceptions must propagate without opening a second backend.

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
launch. It owns a stable `PlatformProfile`, one broad compatibility adapter,
one `DesktopServices` instance, immutable capability results, and feature-gate
decisions. It is not a global singleton: callers receive it by injection, which
keeps test setup deterministic and prevents unrelated GUI surfaces from
repeatedly constructing portal-backed services.

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
    -> pure FeatureDecision policy
    -> immutable update-target capability result
    -> update probe/configuration and existing package adapter
```

The update configuration is resolved when the runtime is composed, rather than
when `update_checker.py` is imported. This means CLI `--update-branch` and
other explicit configuration are seen by the process-owned manager. The
manager rechecks the gate before it starts a check and before it starts a
download; a disabled gate never starts network work. Offline failures remain
ordinary transient check results, not a platform capability failure.

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
reveal action when it is disabled. `PlatformUpdatePackageRevealAdapter` is a
temporary narrow facade over the existing broad adapter methods, deliberately
preserving macOS read-only DMG mounting, Windows Explorer selection, and the
Linux desktop-service fallback.

Verified package persistence has its own focused
`UpdatePackageStorageAdapter`. It is invoked only after checksum verification,
so it has no process-static capability probe or feature gate: a user-visible
storage location can become unavailable while the app is running. Its current
compatibility facade delegates to the broad adapter's established naming,
collision, macOS DMG, and Linux AppImage behavior. A persistence exception is
an ordinary update-workflow failure, after which `UpdateManager` performs its
normal temporary-file cleanup.

Saved-recording reveal is a focused post-save
`SavedRecordingRevealAdapter`. It runs only after the encoder reports success
for a user-visible stop, so it does not require a capability probe or feature
gate: a file-manager failure cannot undo a recording that was already saved.
Its compatibility facade delegates to the broad adapter's established Finder,
Explorer, and Linux desktop-service behavior. The viewer logs a reveal failure
while preserving the successful recording status. Notifications and inhibition
remain separate migrations.

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

`SplashPlatformAdapter` remains a compatibility surface for presentation and
unmigrated platform actions. `UpdatePackageRevealAdapter`,
`UpdatePackageStorageAdapter`, and `SavedRecordingRevealAdapter` are narrow
facades around existing package and post-save actions. New features should add
the smallest appropriate combination of a probe, a pure policy in
`caveviewer.gui.features`, and an injected action adapter rather than
expanding this broad protocol. Cache, chunk streaming, navigation, and map
state are outside this runtime layer.

## Key Components

### `base.py` – Protocol Definition

Defines `SplashPlatformAdapter`, a Protocol that specifies all platform-aware methods:

```python
class SplashPlatformAdapter(Protocol):
    def bookmark_save_modifier(self) -> str:
        """Return 'command' (macOS) or 'control' (Windows/Linux)"""

    def mouse_look_button_name(self) -> str:
        """Return 'right' (macOS) or 'left' (Windows/Linux)"""

    # ... other methods for update metadata, package reveal, UI fonts, etc.
```

**When to modify**: Only for an existing compatibility concern that has not
yet been split. New platform-dependent features use a narrow edge probe, pure
policy, and injected action adapter or service instead of expanding this broad
protocol.

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
creates immutable update capability and feature-decision values. It must not
perform network checks, D-Bus calls, GPU probes, file-manager launches, DMG
mounts, or other expensive on-demand work while it is being composed.

Inject `PlatformRuntime` into a feature service when migrating it. The service
must use the runtime's adapter and `DesktopServices`, rather than construct a
second one. Existing callers of `get_platform_adapter()` and
`get_desktop_services()` remain valid only as compatibility paths until their
concerns are split out of `SplashPlatformAdapter`.

### `macos.py` – macOS Implementations

Extends `DefaultSplashPlatformAdapter` with macOS-specific overrides:

```python
class MacOSSplashPlatformAdapter(DefaultSplashPlatformAdapter):
    def ui_font_family(self) -> str:
        return "Helvetica Neue"

    def bookmark_save_modifier(self) -> str:
        return "command"

    def mouse_look_button_name(self) -> str:
        return "right"

    def install_channel(self) -> str:
        return "macos_app"  # DMG distribution channel
```

### `windows.py` & `linux.py` – Platform Overrides

Similarly extend `DefaultSplashPlatformAdapter` with Windows/Linux specific behavior:

```python
class WindowsSplashPlatformAdapter(DefaultSplashPlatformAdapter):
    def bookmark_save_modifier(self) -> str:
        return "control"

    def mouse_look_button_name(self) -> str:
        return "left"

    def install_channel(self) -> str:
        return "windows_app"  # ZIP distribution channel
```

### `default.py` – Fallback Implementations

Provides safe defaults for methods that should work on all platforms:

```python
class DefaultSplashPlatformAdapter(SplashPlatformAdapter):
    def ui_font_family(self) -> str:
        return "Segoe UI"  # Generic, widely available

    def install_channel(self) -> str:
        return "unsupported"  # Safe default for unknown platforms
```

## Usage Examples

### In application code (e.g., `viewer_window.py`):

```python
from caveviewer.gui.platform.runtime import PlatformRuntime

def configure_viewer(runtime: PlatformRuntime) -> None:
    adapter = runtime.platform_adapter

    # Get platform-specific presentation behavior from the process-owned adapter.
    if adapter.mouse_look_button_name() == "right":
        # macOS: show "Right click + mouse"
        pass

    modifier = adapter.bookmark_save_modifier()  # "command" or "control"
    font = adapter.ui_font_family()  # Platform-specific UI font
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
4. Use `SplashPlatformAdapter` only when migrating an existing compatibility
   method. Do not add unrelated new methods to it; split focused adapters as
   consumers move.
5. Test four boundaries: pure policy table, fake probe/adapter, runtime
   composition and injection, and consumer enforcement/recheck.

## Example: Keyboard Shortcuts

### Problem
macOS uses Cmd+1..9 to save bookmarks; Windows/Linux use Ctrl+1..9.

### Solution

1. **Added to protocol** (`base.py`):
   ```python
   def bookmark_save_modifier(self) -> str:
       """Return the modifier key name ('command' or 'control')."""
   ```

2. **Implemented per-platform** (`macos.py`, `default.py`):
   - macOS: Returns `"command"`
   - Windows/Linux: Returns `"control"`

3. **Used in UI code** (`controls_overlay.py`):
   ```python
   adapter = get_platform_adapter()
   if adapter.bookmark_save_modifier() == "command":
       rows.append(("Cmd + 1..9", "Save camera bookmark slot"))
   else:
       rows.append(("Ctrl + 1..9", "Save camera bookmark slot"))
   ```

## Testing Platform-Specific Code

For unit testing with mocked platforms:

```python
from typing import Protocol

class MockAdapter:
    def bookmark_save_modifier(self) -> str:
        return "command"  # Simulate macOS

    def mouse_look_button_name(self) -> str:
        return "right"

# Inject mock in tests, then verify behavior
```

## Common Patterns

### UI Strings with Platform Differences
**When**: User-facing text that varies by platform (e.g., "Cmd+1" vs "Ctrl+1")

**How**:
1. For an existing presentation concern, query the injected runtime adapter
   for the variable part (for example, `bookmark_save_modifier()`).
2. Build the full string in UI code without adding platform checks.

### Update Packages & Distribution Channels
**When**: Different distribution formats (DMG for macOS, ZIP for Windows, AppImage for Linux)

**How**:
- `install_channel()` returns the channel identifier
- `UpdatePackageStorageAdapter.persist_verified_package()` promotes a verified
  temporary package into platform-specific user-visible storage
- `persist_downloaded_payload()` remains the compatibility implementation until
  storage behavior moves behind that focused adapter
- `download_reveal_action_label()` provides the splash action text
- `reveal_downloaded_payload()` exposes the verified package without running it
- The update system knows which channel the current build came from

Revealing is deliberately manual and non-executing. macOS mounts the DMG
read-only and reveals its `.app` in Finder, Windows selects the package in
Explorer, and Linux uses `OpenURI.OpenDirectory` with `xdg-open` fallback.
Adapters must never launch an installer or execute a downloaded package.

### Saved Files
**When**: A workflow creates a user-owned output file, such as an MP4 recording,
and should show the file in the native file manager after a user-visible
completion.

**How**:
- `SavedRecordingRevealAdapter.reveal_saved_recording()` exposes a completed
  recording without opening or executing it.
- `reveal_file()` remains the compatibility implementation until recording
  reveal behavior moves behind that focused adapter.
- macOS reveals the file in Finder.
- Windows selects the file in Explorer.
- Linux uses desktop-service reveal, with portal support and fallback behavior
  centralized outside viewer code.

Callers should treat reveal as best-effort and keep the primary success state
intact if the file manager cannot be launched.

### UI Framework & Fonts
**When**: Native UI elements (menus, fonts, dialogs) behave differently per-platform

**How**:
- `ui_font_family()` returns platform-appropriate font
- `install_about_handler()` integrates with native About menu on macOS

## Best Practices

1. **Centralize, don't scatter**: Use injected runtime adapters/services for
   migrated features; never add platform checks directly in feature code.
2. **Default to safe**: The `DefaultSplashPlatformAdapter` should provide the most conservative, widely-compatible behavior
3. **Document why**: When platform implementations differ, document the reason in comments
4. **Test both paths**: Verify behavior on at least macOS and Windows/Linux if possible
5. **Keep it simple**: Keep compatibility protocol methods focused and split
   new native actions into narrow adapters rather than creating a god-adapter.

## Related Files

- **`src/caveviewer/gui/controls_overlay.py`**: Uses `bookmark_save_modifier()` and `mouse_look_button_name()` to display platform-specific controls
- **`src/caveviewer/gui/viewer_window.py`**: Uses adapters to bind keyboard/mouse events to platform-specific modifiers
- **`src/caveviewer/gui/update_manager.py`**: Owns update state, persistence, and package reveal
- **`src/caveviewer/gui/splash_screen.py`**: Presents update state and platform action labels
- **`docs/development/releases.md`**: Defines platform release artifacts, update manifest paths, and signing behavior
