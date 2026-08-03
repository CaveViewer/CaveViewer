# Platform-Specific Code Module

This directory implements a **platform adapter pattern** that allows the CaveViewer application to provide platform-specific behavior across macOS, Windows, and Linux without scattering conditional logic throughout the codebase.

## Directory Structure

```
platform/
├── __init__.py              # Public API exports
├── app_identity.py          # Native window identity and Tk root options
├── base.py                  # SplashPlatformAdapter protocol definition
├── desktop_services.py      # Desktop file, URI, notification, and inhibit services
├── runtime.py               # Per-process platform composition and feature gates
├── probes/updates.py        # Static signed-update configuration and target probe
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
have been applied. It owns a stable `PlatformProfile`, one broad compatibility
adapter, one `DesktopServices` instance, immutable capability results, and
feature-gate decisions. It is not a global singleton: callers receive it by
injection, which keeps test setup deterministic and prevents unrelated GUI
surfaces from repeatedly constructing portal-backed services.

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

`SplashPlatformAdapter` remains a compatibility surface for presentation and
unmigrated platform actions. New features should add a narrow probe, a pure
policy in `caveviewer.gui.features`, and an injected action adapter rather than
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

**When to modify**: Add new methods when introducing platform-specific behavior elsewhere in the codebase.

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
perform network checks, D-Bus calls, GPU probes, or other expensive on-demand
work while it is being composed.

Inject `PlatformRuntime` into a feature service when migrating it. Existing
callers of `get_platform_adapter()` and `get_desktop_services()` remain valid
until their concerns are split out of `SplashPlatformAdapter`.

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
from caveviewer.gui.platform.factory import get_platform_adapter

adapter = get_platform_adapter()

# Get platform-specific behavior
if adapter.mouse_look_button_name() == "right":
    # macOS: show "Right click + mouse"
else:
    # Windows/Linux: show "Left click + mouse"

modifier = adapter.bookmark_save_modifier()  # "command" or "control"
font = adapter.ui_font_family()  # Platform-specific UI font
```

No need for `if sys.platform == ...` checks anywhere in the main code!

## How to Add Platform-Specific Functionality

### Step 1: Add Method to Protocol (`base.py`)

Define the method contract with documentation:

```python
def my_new_feature(self) -> str:
    """Return platform-specific value for my_new_feature.

    macOS example: 'value_for_mac'
    Windows/Linux example: 'value_for_others'
    """
    ...
```

### Step 2: Implement the Default and Required Overrides

Add shared behavior to `default.py`, then override only the platforms that
need different behavior. The protocol records the complete structural
contract.

**`macos.py`:**
```python
def my_new_feature(self) -> str:
    return "value_for_mac"
```

**`windows.py` (only when Windows differs from the default):**
```python
def my_new_feature(self) -> str:
    return "value_for_windows"
```

**`linux.py` (only when Linux differs from the default):**
```python
def my_new_feature(self) -> str:
    return "value_for_linux"
```

**`default.py`:**
```python
def my_new_feature(self) -> str:
    return "fallback_value"  # Safe default
```

### Step 3: Use in Application Code

```python
adapter = get_platform_adapter()
my_value = adapter.my_new_feature()
```

That's it! The platform-specific behavior is now centralized and accessible from anywhere in the codebase.

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
1. Add method to protocol that returns the variable part (e.g., `bookmark_save_modifier()`)
2. In UI code, query adapter and build the full string dynamically

### Update Packages & Distribution Channels
**When**: Different distribution formats (DMG for macOS, ZIP for Windows, AppImage for Linux)

**How**:
- `install_channel()` returns the channel identifier
- `persist_downloaded_payload()` implements platform-specific storage
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
- `reveal_file()` exposes a saved file without opening or executing it.
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

1. **Centralize, don't scatter**: Always use the adapter, never add platform checks directly in feature code
2. **Default to safe**: The `DefaultSplashPlatformAdapter` should provide the most conservative, widely-compatible behavior
3. **Document why**: When platform implementations differ, document the reason in comments
4. **Test both paths**: Verify behavior on at least macOS and Windows/Linux if possible
5. **Keep it simple**: Protocol methods should do one thing; don't create god-adapters with 50 unrelated methods

## Related Files

- **`src/caveviewer/gui/controls_overlay.py`**: Uses `bookmark_save_modifier()` and `mouse_look_button_name()` to display platform-specific controls
- **`src/caveviewer/gui/viewer_window.py`**: Uses adapters to bind keyboard/mouse events to platform-specific modifiers
- **`src/caveviewer/gui/update_manager.py`**: Owns update state, persistence, and package reveal
- **`src/caveviewer/gui/splash_screen.py`**: Presents update state and platform action labels
- **`docs/development/releases.md`**: Defines platform release artifacts, update manifest paths, and signing behavior
