# Main-window Help

## Goal

Add a **Help** section to the splash/main-window navigation rail directly
above **About**. Its **Keys** tab is a platform-correct, visually scannable
reference for CaveViewer's direct viewer key bindings.

The page complements the viewer's existing controls overlay. It does not add
or change a keyboard shortcut.

## User experience

The navigation rail is:

```text
Map Library
Preferences
Help
About
```

Selecting **Help** opens an embedded right-hand surface with one selected tab,
**Keys**. The tab contains one vertically scrollable table:

- group dividers such as **Movement**, **View**, **Bookmarks**, **Map**,
  **Map Import**, **Recorded Dive**, and **Capture**;
- compact keycap chips in a fixed left column;
- a consistently aligned action column; and
- no page title, explanatory subtitle, or `Keys` / `Action` column headings.

The tab label and table structure are sufficient to explain the content. Keep
the type scale restrained: group dividers are small and muted, keycaps and
actions use the same body-scale hierarchy, and the table—not a large heading—
owns visual attention. Use the splash dark theme, amber only for the selected
tab, and a narrow themed scrollbar when content overflows.

Use the active presentation profile for labels: **Ctrl** on Windows/Linux and
**Cmd** on macOS. Do not query `sys.platform` directly from the panel.

Leaving Help follows the same navigation rules as About. Its own scrolling
must not resize or re-center the splash window, and leaving unsaved
Preferences for Help must retain the existing discard confirmation.

## Binding inventory

Only show bindings with one direct, specific viewer action. Do not represent
focus- or active-surface-dependent splash behavior as a universal binding.
In particular, omit `Ctrl`/`Cmd` + `W`, generic `Return`, generic `Escape`,
and `Tab` rows.

| Group | Key binding | Action |
| --- | --- | --- |
| Movement | `W` `A` `S` `D` | Move forward, left, backward, and right. |
| Movement | `E` / `Q` | Move up / down. |
| Movement | `Shift` | Speed boost. |
| Movement | `-` / `=` | Decrease / increase fly speed. |
| View | Arrow keys or `J` `L` `I` `K` | Look left, right, up, and down. |
| View | `Z` / `X` | Roll left / right. |
| View | primary modifier + `0` | Reset view (level horizon). |
| Bookmarks | platform bookmark modifier + `1`–`9` | Save camera bookmark. |
| Bookmarks | `Shift` + `1`–`9` on macOS | Save camera bookmark fallback. |
| Bookmarks | `1`–`9` | Recall camera bookmark. |
| Bookmarks | `Delete` + `1`–`9` or `Ctrl` + `Shift` + `1`–`9` | Delete camera bookmark. |
| Map | primary modifier + `O` | Open another map. |
| Map Import | primary modifier + `Shift` + `P` | Pause active import. |
| Recorded Dive | `Space` | Pause/resume Recorded Dive. |
| Capture | primary modifier + `R` | Start/stop recording. |
| Capture | primary modifier + `T` | Start/stop manual trace. |
| Capture | primary modifier + `C` | Start/stop slice. |
| Capture | `Escape` | Cancel active slice. |

For recording, manual tracing, and slicing, preserve the established
`<shortcut> start/stop <feature>` action wording. Do not mention the removed
REC button.

Mouse gestures, minimap clicks, and visible buttons remain valid viewer
controls, but they are not part of the Keys table.

## Implementation

### Shared catalog

`caveviewer.gui.controls_catalog` owns immutable key-binding sections and a
single `keyboard_control_sections()` function. It is pure and
presentation-profile-aware, but imports neither Tk nor ModernGL. It also owns
the pure keycap-token splitter used by both the Tk table and OpenGL overlay.

The catalog represents direct viewer bindings only. Context-sensitive splash
navigation remains implemented in `splash_screen.py` and is intentionally not
shown as a simplified universal shortcut.

The viewer controls overlay derives its keyboard rows from the catalog, then
adds its overlay-specific mouse, minimap, and visible-button rows. It may
combine catalog sections to suit its compact three-column layout, but must not
duplicate the underlying binding text.

### Keys table

`caveviewer.gui.help_panel.HelpPanel` is a Tk presentation component. It
builds inside the splash's existing right-hand surface and never creates a
`Tk` or `Toplevel`. It renders the selected **Keys** tab and one aligned table
inside a canvas-backed scroll container. The panel owns no shortcut dispatch.

### Splash integration

`show_splash_screen` lazily creates one Help panel using the immutable
presentation profile. Help remains between Preferences and About, uses the
existing unsaved-Preferences guard, and returns to Map Library through the
same root navigation flow as About.

## Tests

Keep deterministic GUI-unit coverage for:

- the catalog's complete direct-binding id set and Ctrl/Cmd labels;
- the absence of stale `Ctrl`/`Cmd` + `W` and generic contextual rows;
- the exact `W A S D` action text, capture start/stop wording, Map Import
  section, and precise `Escape` slice-cancel action;
- pure keycap tokenization for key groups and modifier combinations;
- the viewer overlay consuming the shared catalog rather than a duplicate;
- the Keys tab/table structure, scrollbar overflow behavior, and absence of
  redundant page, subtitle, and column labels; and
- Help's position in the splash rail and existing Preferences safeguards.

Run the focused GUI tests, then the complete suite and `git diff --check`
before handoff.

## Acceptance criteria

- Help appears above About and shows the selected **Keys** tab.
- The table is compact, aligned, scrollable, and visually consistent with the
  splash rather than a stack of unrelated cards.
- Every displayed row maps to a real direct binding, with correct Ctrl/Cmd
  labels and no stale `Ctrl`/`Cmd` + `W` claim.
- `W A S D` reads “Move forward, left, backward, and right.”
- Map Import is a separate section containing the import-pause binding.
- No new shortcut, dependency, cache format, or platform-specific input path
  is introduced.
