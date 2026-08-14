# Main-window Help

## Goal

Add a **Help** section to the splash/main window navigation rail directly
above **About**. It gives users one complete, platform-correct reference for
every supported keyboard command before they open a cave.

The section complements the viewer's existing Help overlay; it does not add a
new shortcut or remove the in-viewer Help button.

## User experience

The navigation rail becomes:

```text
Map Library
Preferences
Help
About
```

Selecting **Help** opens an embedded right-hand surface titled **Keyboard
shortcuts**. It contains compact, grouped sections in two columns on wide
windows and a vertically scrollable single column when the available width is
narrow. The shortcut is visually distinct from its action, and a secondary note
states any applicable condition rather than hiding the row. For example,
`Ctrl + Shift + P` appears with **Pause an active map import** even though it
only takes effect during an import.

Use the active presentation profile for labels: **Ctrl** on Windows/Linux and
**Cmd** on macOS. Do not query `sys.platform` directly from the panel.

Leaving Help follows the same navigation rules as About:

- Clicking **Map Library**, pressing `Escape`, or pressing the primary
  close-window shortcut returns to Map Library.
- Moving from unsaved Preferences to Help goes through the existing discard
  confirmation.
- `Tab` / `Shift+Tab` move focus, and `Return` / `Space` activate the focused
  navigation item or action.

The page must remain usable at the splash window's minimum height. Its own
content scrolls; opening Help must not resize or re-center the splash window.

## Shortcut inventory

The initial page must cover the following keyboard input, including the
context that changes an otherwise shared key's behavior.

| Group | Shortcut | Action / condition |
| --- | --- | --- |
| Main window | `Tab` / `Shift+Tab` | Move keyboard focus between controls. |
| Main window | `Return` / `Space` | Activate the focused control. |
| Main window | `Return` | Open a local map when Map Library owns the active surface and no focused control consumes it. |
| Main window | `Escape` / primary modifier + `W` | Return from Help or About; cancel Preferences with the existing discard check; otherwise close the splash. |
| Move | `W` `A` `S` `D` | Move forward, left, backward, and right. |
| Move | `E` / `Q` | Move up / down. |
| Move | Hold `Shift` | Temporary speed boost. |
| Move | `-` / `=` | Decrease / increase the persistent fly speed. |
| Look | Arrow keys or `J` `L` `I` `K` | Look left, right, up, and down. |
| Look | `Z` / `X` | Roll left / right. |
| View | primary modifier + `0` | Reset the view and level the horizon. |
| Bookmarks | primary modifier + `1`–`9` | Save camera bookmark slot. macOS also accepts the existing `Shift` + digit fallback when Command is not reported by the backend. |
| Bookmarks | `1`–`9` | Recall camera bookmark slot. |
| Bookmarks | `Delete` + `1`–`9` | Delete camera bookmark slot. |
| Map | primary modifier + `O` | Switch to a different map. |
| Startup / Recorded Dive | `Space` | Begin after the startup controls screen is ready; pause/resume an active Recorded Dive. |
| Capture | primary modifier + `R` | Start/stop recording. A second press during the countdown cancels it. |
| Capture | primary modifier + `T` | Start/stop manual trace. A second press during the countdown cancels it. |
| Capture | primary modifier + `C` | Start/stop slice. A second press during the countdown cancels it; once active, it finishes and saves the slice. |
| Capture | `Escape` | Cancel a user-owned slice before publication; otherwise close the viewer. |
| Import | primary modifier + `Shift` + `P` | Pause an active map import. |

For the three capture rows, keep the established help wording in the
`<shortcut> start/stop <feature>` form. Do not mention removed controls such
as the REC button.

Mouse gestures, minimap clicks, and visible buttons are not keyboard
shortcuts. They may remain in the viewer's existing visual-control overlay,
but they do not belong in this page's keyboard inventory.

## Implementation

### 1. Make the shortcut list a shared, pure catalog

Create `caveviewer.gui.controls_catalog`. It owns immutable section and row
models and a single public function such as:

```python
def keyboard_control_sections(
    presentation_profile: PresentationProfile,
) -> tuple[KeyboardControlSection, ...]: ...
```

Each row should have a stable identifier, a rendered shortcut label, primary
action text, and optional context note. The catalog is presentation-profile
aware but must not import Tk, ModernGL, or `viewer_window`.

Move the current pure data construction from
`controls_overlay._get_platform_control_sections()` into that module, then
extend it with the omitted arrow-key, import-pause, startup, and splash-window
rows. Have the viewer overlay consume the same catalog for its keyboard rows.
This creates one source of truth and prevents a future shortcut change from
updating either the overlay or main-window Help while missing the other.

Keep mouse and visible-control entries in a separate visual-controls catalog
or overlay-only list so the new Help surface stays scoped to keyboard input.

### 2. Add an embedded Help panel

Create `caveviewer.gui.help_panel` as a Tk presentation component. It should:

- build inside the splash's existing `right_frame`, never create a `Tk` or
  `Toplevel`;
- accept a profile-resolved shared catalog, splash typography/style tokens, and
  scale function rather than reaching into viewer state;
- present semantic section headings and aligned shortcut/action rows;
- use a canvas-backed scroll container with a visible scrollbar only when the
  content overflows; and
- provide `focus_content()` so the splash can put keyboard focus inside the
  page after navigation.

The panel is presentation-only. Shortcut dispatch remains in
`viewer_window.py`, and the catalog does not execute actions.

### 3. Wire the section into the splash navigator

In `show_splash_screen`:

1. Create `help_surface` next to `map_library_surface`,
   `preferences_surface`, `about_surface`, and `cave_metadata_surface`.
2. Lazily create one `HelpPanel` with the immutable splash presentation
   profile, as About is created today.
3. Add a **Help** navigation item after **Preferences** and before **About**.
4. Add `_show_help_surface()` and `_on_help_click()`; the click handler must
   use `_request_leave_preferences()`.
5. Include `help_surface.pack_forget()` in every existing surface transition,
   including cave metadata. Mark **Help** selected only while it is active.
6. Treat `active_surface == "help"` like About in the root `Return` and
   `Escape` handlers, returning to Map Library rather than opening a map or
   closing the splash.

The rail remains fixed-width and the existing Map Library, Preferences, About,
and cave-metadata behavior must not change.

## Tests

Add deterministic GUI-unit coverage for:

- the shared catalog's complete stable-id set and the exact Ctrl/Cmd labels
  for Windows/Linux and macOS;
- every capture row using the standardized `start/stop` wording, including
  slice's finish-and-save context;
- the viewer overlay consuming the shared keyboard catalog rather than a
  duplicate local list;
- Help's rail position between Preferences and About, active styling, and
  surface switching;
- leaving unsaved Preferences for Help showing the existing discard flow;
- `Escape`, `Return`, `Space`, and the primary close shortcut preserving the
  specified main-window behavior; and
- overflow/scroll behavior without constructing a real display where a
  layout calculation can be tested separately.

Run at least:

```bash
PYTHONPYCACHEPREFIX=/tmp/caveviewer-pycache \
  .venv-dev/bin/python -m pytest -p no:cacheprovider -q \
  tests/unit/gui/test_controls_overlay.py \
  tests/unit/gui/test_splash_update_presentation.py \
  tests/unit/gui/test_viewer_window.py
```

Then run the full suite and `git diff --check` before handoff.

## Acceptance criteria

- Help appears above About in the main-window navigation rail.
- It covers every keyboard command listed above with platform-correct labels.
- The viewer overlay and main-window page get their keyboard rows from one
  pure catalog.
- Help is keyboard-accessible, scrolls instead of resizing the splash, and
  does not interfere with unsaved Preferences safeguards.
- No new shortcut, dependency, cache format, or platform-specific input path
  is introduced.
