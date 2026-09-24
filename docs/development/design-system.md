# Design system

This document defines the small, shared visual system for CaveViewer-owned
interfaces. It applies to the Tk startup shell, Map Library, cave details,
About, Preferences, and the OpenGL viewer's loading and capture feedback.
Native operating-system window chrome is not part of this system; the platform
owns its title-bar typography.

## Typography

`src/caveviewer/gui/tk_typography.py` is the canonical source for shared Tk
logical-pixel roles. Measured surfaces define their exact logical-pixel sizes in
`preferences_style.py`, `help_style.py`, and `map_library_style.py`. Components
receive those roles through their presentation style or constructor rather
than choosing a font size for an individual widget.

CaveViewer bundles the official hinted static Inter 4.0 Regular, Medium,
SemiBold, and Bold TrueType faces. The application registers them for the
current process
before it creates the Tk shell; it never installs fonts into system or user font
directories. Tk uses `Inter` for regular and bold semantic roles and the
registered `Inter Medium` and `Inter SemiBold` faces for their presentation
roles. If native
registration or Tk discovery fails, the presentation profile falls back to
Segoe UI on Windows, Helvetica Neue on macOS, and the active sans-serif family
on Linux without blocking startup.

The OpenGL bitmap renderer loads the same bundled Inter Regular file directly
through FreeType. An explicit runtime font path or `CAVEVIEWER_UI_FONT` retains
its existing priority, followed by bundled Inter and the platform system-font
candidates. Missing or invalid candidates are skipped rather than making an
otherwise usable fallback unreachable.

Every size below is a logical-pixel base size before the active accessibility
and display scales are applied. Application-owned Tk font tuples always use a
negative size because Tk interprets negative font sizes as pixels. Positive Tk
font sizes are points and are not allowed for CaveViewer typography.

| Shared role | Base size | Weight | Use |
| --- | ---: | --- | --- |
| `display` | 24 px | Bold | One primary page subject, such as a cave name. |
| `heading` | 19 px | Bold | App identity and top-level panel headings. |
| `body_strong` | 13 px | Bold | Primary actions, selected values, and compact headings. |
| `body` | 13 px | Regular | Prose, editable controls, links, and menu actions. |
| `supporting` | 12 px | Regular | Locations, descriptions, version text, hints, statuses, and disclaimers. |
| `section` | 12 px | Bold | Section labels such as **CaveViewer Maps**, **Key facts**, and **Source**. |

The following table is the complete logical-pixel size contract for the Tk
shell and its card-based surfaces:

| Surface and role | Base size | Weight | Line height |
| --- | ---: | --- | ---: |
| Navigation label | 13 px | Regular; SemiBold when active | Native |
| Navigation update/status text | 13 px | Medium | Native |
| Map Library section heading | 15 px | Bold | Native |
| Map Library map title | 14 px | SemiBold | Native |
| Map Library local-map action | 14 px | Regular | Native |
| Map Library description, status, and file size | 12 px | Regular | Native |
| Map Library menu item | 13 px | Regular; SemiBold when selected | Native |
| Preferences and Help tab | 14 px | Regular; SemiBold when active | Native |
| Preferences and Help card title | 16 px | Bold | Native |
| Preferences and Help card description | 13 px | Regular | Native |
| Preferences field label | 14 px | SemiBold | Native |
| Preferences field description | 12 px | Regular | 16.8 px (140%) |
| Preferences input value | 14 px | Medium | Native |
| Preferences unit | 14 px | Regular | Native |
| Help action | 14 px | SemiBold | Native |
| Help detail and error text | 12 px | Regular | Native |
| Help keycap | 14 px | Medium | Native |
| Unsaved Preferences title | 22 px | Bold | Native |
| Unsaved Preferences description | 15 px | Regular | 22 px |
| Unsaved Preferences Edit and Discard actions | 14 px | Medium | Native |
| Unsaved Preferences Save action | 15 px | Bold | Native |

The Map Library section heading is 80% of the 19-pixel `heading` role and
resolves to 15 logical pixels at the base scale. About, cave details, launch
text, feedback, and shared message dialogs use the shared logical-pixel roles
above; their exact role assignments are listed below.

CaveViewer applies the active text scale once to every logical-pixel role,
except for the reviewed fixed 14 px and 15 px action labels in the unsaved
Preferences dialog. It then applies the same display scale used by logical-pixel
geometry exactly once and rounds the final Tk pixel size. Windows and macOS
derive accessibility text scale from `max(1.0, TkDefaultFont points / 12)`;
Linux retains `1.0` because its desktop configuration is already represented by
the resolved display scale. A fallback font may change the typeface and font
metrics, but it must not replace any base size in these tables.

On Windows, logical-pixel geometry and typography use the effective window DPI
divided by 96 exactly once. The shell then applies the same bounded
physical-density factor to fonts and geometry: monitors up to
24 inches retain `1.00`, larger monitors use `clamp(24 / diagonal_inches,
0.95, 1.00)`, and invalid or unavailable raw measurements retain `1.00`. This
keeps monitors of approximately 25.3 inches and larger at the same modest
density floor. On a settled Windows monitor transition, CaveViewer recomputes
the factor and rebuilds the
retained Tk shell off-screen so semantic fonts and logical geometry adopt the
destination scale together. Before content fitting, the retained root is
mapped invisibly on the destination display so Tk child geometry reflects the
new monitor; impossible measurements such as a viewport taller than its root
are rejected. The current outer window size changes by the same
scale ratio. Normal windows retain at least the destination's 1040-by-740
logical-pixel default when the work area permits, larger user sizing is
preserved proportionally, and maximized windows remain maximized. All resulting
bounds are clamped to the destination work area. Tk's retained interpreter may
still synchronize its native point scale during monitor recomposition for
platform-owned defaults, but CaveViewer font tokens remain negative pixel sizes
and do not depend on that conversion. Window geometry follows the same
single-conversion rule: retain the last
settled source-monitor normal bounds, apply the destination/source layout-scale
ratio once, and use Windows' settled destination position. Do not multiply
dimensions that Windows or Tk has already adjusted for the destination DPI.

The development-only `CAVEVIEWER_TK_SCALE` override retains its historical Tk
pixels-per-point input but is converted to the same logical display scale; while
active, it also bypasses adaptive density. CaveViewer does not expose a
persisted UI-size preference. macOS, Linux, and OpenGL surfaces do not use the
Windows physical-density factor.

### Role mapping

- Map Library rows use the measured 14-pixel semibold map-title role and the
  12-pixel regular supporting role for location, availability, and cache
  status. Map names use all width remaining after fixed trailing columns for
  size, primary action, and overflow action; they wrap only when the live window
  width truly requires it, rather than at a fixed column width. Text never
  displaces or clips those controls.
- The **Open a local map** card uses the measured 14-pixel regular action role
  and 12-pixel regular supporting role.
- Primary-shell navigation uses an exact 13-logical-pixel label. Inactive items
  use the platform regular face; the active item uses the platform semibold
  face where available and a bold fallback elsewhere.
- Cave details use `display` only for the cave name, `body` for facts and
  sources, `body_strong` for statistic values, `supporting` for its location
  and disclaimer, and `section` for section labels.
- Preferences and Help use the measured roles in the logical-pixel table. Their
  role names describe hierarchy and emphasis; they do not inherit the numeric
  sizes of similarly named shared roles.
- About uses `heading` for the product name, `supporting` for version text and
  the copyright mark, `body` for credits and links, and `body_strong` for its
  close action when one is shown.
- Shared message dialogs use `body_strong` for the heading and actions, `body`
  for the message, and `supporting` for secondary status text.
- Tk and OpenGL Help keycaps use geometric unit spans. One unit (`1u`) is the
  standard single-key cap; an `n`-unit cap is exactly `n` single-key widths plus
  the `n - 1` ordinary gaps those keys would contain as a row. Compact named
  keys (`Cmd`, `Ctrl`, `Del`, `Scroll`, `Shift`, `1–9`, `Escape`, and `Space`)
  are `2u`, so a standalone `Shift` aligns with an `E Q` row and `Escape`
  aligns with `Space`. Center labels both horizontally and vertically within
  their keycaps: Tk uses its centered canvas anchor, while OpenGL centers the
  bitmap font's tight rendered bounds. A compound separator occupies one full
  borderless 1u cell, with the ordinary inter-key gap on each side. This keeps
  a `2u + 1u separator + 1u` shortcut aligned to the same four-column grid as
  four adjacent 1u keys.

Text hierarchy should come first from role, then from color and spacing.
Do not create a new font size merely to distinguish a control state; use the
appropriate weight, color, or interaction treatment instead.

## Application shell and Map Library visual contract

The primary shell uses `#0D0F13` as its application surface and `#15171C` as
the shared panel/card fill for Map Library, Preferences, Help, and About. The
main content column starts 32 logical pixels after the 220-pixel sidebar and
retains a 32-pixel right gutter in the 1080-by-740 reference frame. This makes
the reference content column 796 pixels wide, from x=252 through x=1048. Map
Library begins 24 pixels below the top edge; tabbed surfaces begin with a
16-pixel inset, a 44-pixel tab row, and a 24-pixel content gap. The 32-pixel
right gutter reserves a 16-pixel scrollbar rail when a surface scrolls.

Map Library owns one immutable, scale-once metrics snapshot in
`caveviewer.gui.map_library_style`. Every value below is a logical pixel and is
converted by the active display-scale helper exactly once.

| Relationship | Logical value |
| --- | ---: |
| Recent/catalog card corner radius | 10 |
| Card border | 1 |
| Card horizontal padding | 24 |
| Card top / bottom padding | 24 / 24 |
| Gap between cards | 24 |
| Recent card minimum height | 138 |
| Catalog card minimum height | 484 |
| Empty local-map icon | 32 |
| Populated local-map icon | 20 |
| Populated local-map action height | 28 |
| Catalog row gap | 8 |
| Primary / overflow action target | 28 / 24 |
| Reserved progress lane / preceding gap | 3 / 5 |

Both cards use `#15171C`. The Recent border is `#30343D`; the catalog border is
`#313337`. Map titles use 14-pixel semibold `#EFF1F5`, descriptions use
12-pixel regular `#B6BCC8`, and file sizes use 12-pixel regular `#A9AFBC`.
Disclosure and primary row actions use `#F5C451`; overflow uses `#A9AFBC`.
Section headings use 15 logical pixels bold at the base text scale.
Their disclosure triangles span 10 by 5 logical pixels when expanded and 5 by
10 when collapsed, centered vertically beside the title.

The local-map folder is a bundled transparent asset resized with high-quality
downsampling and tinted at render time. It uses `#F5C451` beside the two-line
empty-state action and `#A9AFBC` beside **Open another local map** after recent
rows. Asset pixels do not define hit targets, spacing, state, or behavior.

Map overflow menus use a 204-logical-pixel body with 40-pixel rows, an 8-pixel
corner radius and a 1-pixel inside border in `#30343D`. Rows have 16-pixel
left and right insets, 16-pixel centered icons and a 12-pixel icon-to-label gap.
The 144-pixel label area fits the current action labels on one line in regular
and selected states.
The trash icon is approximately 14 pixels wide and 14 1/3 pixels high, with a
2-pixel stroke. Its top and bottom are inset by half a pixel from the original
artwork, preserving its center and rounded bottom corners.
The About icon uses the SVG's centered 2-pixel stroke on a circle of radius
6 2/3 pixels, giving an outer diameter of 15 1/3 pixels within its 16-pixel slot.
The Remove cache eraser uses 130% of the original path geometry around the slot
center, with a 2.2-pixel stroke. Its visible bounds are approximately 15.2 by
13.9 pixels, retaining the same 16-pixel slot and label spacing.
Dividers separate cache, cave-information and removal groups in that order;
unavailable groups are omitted. Optional map actions remain before the final
removal group. The four-action reference is 162 pixels high. Additional contextual
actions retain the same row geometry. Label wrapping is disabled.

Menu text is native, 13-pixel Inter regular `#A9AFBC`. Hover and keyboard focus
use `#1B1E25` with semibold amber `#F5C451` text and icons. Disabled actions
retain muted text and expose their reason on hover or focus. The shadow uses
an 8-pixel downward offset, 24-pixel blur, negative 8-pixel spread and 40% black,
rendered with transparency outside the rounded body. Its backing follows the
actual card borders and dark application background, clipped to the library
viewport, so crossing a card edge does not reveal a rectangular fill. The
backing stays fixed while menu rows scroll. Moving or resizing the underlying
library dismisses the menu; reopening resolves the new geometry.
Menus open with focus on the container and no selected row. Hover or keyboard
navigation selects a row; Return/Space do nothing until a row is selected.
Arrow keys, Home/End and Tab navigate; Return/Space activate; Escape dismisses
and returns focus to the opener. Outside clicks and focus departure dismiss.
When window height constrains the popover, rows scroll and keyboard focus keeps
the current row visible.

## Preferences visual contract

`caveviewer.gui.preferences_style` is the canonical source for Preferences
geometry, semantic palette mappings, and typography-role names. Its metrics are
logical pixels. A composed Preferences surface converts the complete metrics
snapshot through its active display-scale helper exactly once; scaled metrics
cannot be scaled again.

| Relationship | Logical value |
| --- | ---: |
| Control corner radius | 4 |
| Section-card corner radius | 10 |
| Section-card horizontal and vertical padding | 24 |
| Gap between section cards | 24 |
| Card heading to first field | 24 |
| Card heading to section description | 6 |
| Section description to divider | 20 |
| Header divider thickness | 1 |
| Header divider to content | 24 |
| Field label to description | 4 |
| Field description to control row | 8 |
| Action and compound path-control height | 40 |
| Numeric-control height | 36 |
| Compact numeric-control width | 90 |
| Minimum prominent action width | 160 |
| Control horizontal content padding | 12 |
| Numeric-control vertical content padding | 8 |
| Inline unit gap | 12 |
| Gap between fields or actions in one card | 20 |
| Compound-control seam thickness | 1 |
| Preferences footer top gap | 32 |
| Gap between footer actions | 16 |
| Prominent footer-action height | 44 |
| Ordinary / focused control border | 1 / 2 |

Preferences keeps its four text tabs outside the cards. Tabs remain
keyboard-focusable navigation and do not become pills or cards. A two-logical-
pixel `#30343D` bar matching the label width marks the active tab. Its text
roles use display-scaled logical pixels and the registered static Inter faces:

The top tab row begins at the same 16-logical-pixel inset as the first sidebar
entry. Both rows are 44 logical pixels tall, and their labels use middle
alignment so their text midlines remain aligned at every display scale. Help
uses the same tab-row alignment.

The first tab label aligns with the card's left edge in Preferences and Help.
Keep horizontal spacing between tabs rather than inside their labels, with
only the keyboard focus outline occupying the label's edge.

| Preferences role | Inter weight | Logical size | Color |
| --- | --- | --- | --- |
| Active tab | SemiBold | 14 px | `#EFF1F5` |
| Inactive tab | Regular | 14 px | `#A9AFBC` |
| Section title | Bold | 16 px | `#EFF1F5` |
| Section summary | Regular | 13 px | `#A9AFBC` |
| Field label | SemiBold | 14 px | `#EFF1F5` |
| Field description | Regular | 12 px | `#A9AFBC` |
| Input value | Medium | 14 px | `#EFF1F5` |
| Unit | Regular | 14 px | `#A9AFBC` |

Field descriptions use 140% line height (16.8 logical pixels at the default
12-pixel size), with display and accessibility scaling applied once. The
read-only description component uses native word wrapping and adds leading
between wrapped lines and around each paragraph. Its height follows the text
when the panel is resized; it does not enter keyboard focus order or consume
the panel's wheel scrolling. A taller fallback font is never clipped.

Each preference group is a subtly raised panel-colored card with a
10-logical-pixel radius. Cards share one left and right edge beneath the tabs,
use title-case section-title labels, and remain separated by a 24-logical-pixel
vertical gap. Every card has a one-logical-pixel `#30343D` border, including
Storage and Backup cards. Amber decoration is reserved for primary actions
rather than section surfaces. Streaming uses **Memory Use**, **CPU Use**, and **Frame
Loading** headings. Each has one section-summary purpose line beneath its
heading. Import uses **Map Processing** and **CPU Use** with the
same purpose-line treatment. Storage uses **Locations** with a short purpose
line that introduces the group without repeating either field description.
Backup uses **Save & Load** and **Reset**, each with a short plain-language
purpose line beneath its heading.

One `#30343D` header divider spans the content width beneath each purpose
line. Its position follows the wrapped description rather than a fixed card
coordinate. Card height grows with its content. Keep units outside each input
border; do not add a border around the field group.

The reference card is 796 by 423 logical pixels, with 748 pixels of content
between equal 24-pixel insets. A single-line numeric field is approximately
82 pixels tall; longer descriptions and different content grow naturally.
Preferences and Help use the Map Library column edges without additional
tab-content insets. All three reserve the same scrollbar rail, so cards keep
their width when scrolling becomes necessary.

| Tab | Card heading | Purpose line |
| --- | --- | --- |
| Streaming | **Memory Use** | Control system and graphics memory limits. |
| Streaming | **CPU Use** | Control processor capacity used for loading. |
| Streaming | **Frame Loading** | Control how much map data is loaded per frame. |
| Import | **Map Processing** | Control how map data is divided and processed during import. |
| Import | **CPU Use** | Control processor capacity used to prepare imported map data. |
| Storage | **Locations** | Manage where local files are kept. |
| Backup | **Save & Load** | Keep a copy of your preferences or use one saved earlier. |
| Backup | **Reset** | Return import and streaming preferences to their default values. |

Each ordinary field stacks a semibold label, a regular description, and one
control row. Numeric entries remain compact and left-aligned; a semantic unit
such as `%`, `GB`, or `ms` follows the entry on the same baseline.
The entry placeholder carries its acceptable value range, so the field does
not repeat that range beneath the control. Consecutive fields and actions use
a compact 20-logical-pixel gap without a divider. The final field has no
explanatory footer beneath it.

The Preferences footer follows the measured page viewport edge, including the
space reserved for the scrollbar rail. The right border
of **Save changes** aligns with the right edge of the cards on every tab and
at every display scale.

Entries and action buttons use the 4-logical-pixel radius. Their normal, hover,
pressed, focused, invalid, read-only, and disabled colors resolve from
`TkTheme`; Preferences does not own duplicate color literals. Panel entries use
the semantic panel-entry fill `#0D0F13` and border `#30343D`. Their embedded
native content reserves the thickest outline on all sides, keeping the full
border visible without moving the text when focus changes. Focus uses the
entry focus role and a two-logical-pixel border, while invalid state uses the
semantic invalid border together with persistent field feedback. A directory
path and **Browse** remain one compound control with one rounded outer border
and an internal seam.

The stable Preferences footer keeps **Discard changes** and **Save changes**
right-aligned. Clean or invalid state uses the neutral disabled treatment;
valid pending edits enable the actions without moving the footer or form
content.

The unsaved Preferences confirmation follows the shared 4-pixel grid: a
560-logical-pixel-wide panel with a 220-logical-pixel minimum height, a
10-pixel radius, `#15171C` fill and one-pixel
`#30343D` border. Its 22-pixel Inter Bold heading uses `#F3A812`; the explanation
uses 15-pixel Inter Regular `#A9AFBC` with 22-pixel line height. Text starts
24 pixels from the top and sides. A 32-pixel spacer separates the description
from three equal 160-by-44 actions. The actions have 4-pixel corners, 16-pixel
gaps, 24-pixel side insets and a 24-pixel bottom inset.
The title, description and Edit button share the same left edge. Window width
equals the three button widths plus the two gaps and equal side padding.
**Edit** and **Discard** use `#2A2D35` with 14-pixel Medium `#C8CBD0` text;
**Save** uses `#F3A812` with 15-pixel Bold `#1A1A1A` text. The window grows
for enlarged text and fallback fonts. Native window chrome and shadow remain
platform-owned; this reference uses a text heading without a separate icon.
**Edit**, Escape and native close return to the unsaved form. **Discard**
discards and proceeds; **Save** proceeds only after a successful save. Save
is not focused automatically; all actions retain keyboard traversal, visible
keyboard focus and keyboard activation.

## Help visual contract

`caveviewer.gui.help_style` is the canonical source for Help geometry,
semantic palette mappings, exact typography, and card purpose lines. Help
shares the Preferences 4-logical-pixel control radius, 10-logical-pixel card
radius, 24-logical-pixel card padding and card gaps, and header-divider
spacing. Help keeps 20-logical-pixel gaps between fields and rows. Help-only
canvas measurements define the keycap and action lanes, responsive stacking,
error excerpt, and bottom scroll padding. The active shell display-scale helper
converts every logical metric exactly once.

Help keeps **Keys**, **Capture**, and **Troubleshooting** as text tabs above the
cards. Its tab bar matches Preferences: active text is Inter Semi Bold at 14
logical pixels in `#EFF1F5`, inactive text is Inter Regular at 14 logical pixels
in `#A9AFBC`, and a two-logical-pixel `#30343D` underline matches the active
label width. Tabs remain keyboard-focusable and use no pill or card. The first
Help tab and first Preferences tab share the same top-left origin, including on
a density-adjusted Windows display.

Every Help group is one subtly raised panel-colored card with a title-case
Inter Bold 16-logical-pixel heading in `#EFF1F5` and one short Inter Regular
13-logical-pixel purpose line in `#A9AFBC`. Actions use Inter Semi Bold at 14
logical pixels, details use Inter Regular at 12 logical pixels, and keycaps use
Inter Medium at 14 logical pixels. Cards share the Preferences content edges,
use one `#30343D` divider beneath each purpose line, and remain separated by
24 logical pixels. Keep shortcut and action rows separated by whitespace. At the ordinary
shell width, descriptions should occupy no more than two lines; shorten the copy
instead of reducing type size or card spacing.

| Tab | Card heading | Purpose line |
| --- | --- | --- |
| Keys | **Move** | Control movement direction and speed. |
| Keys | **Look** | Control the direction and orientation of the view. |
| Keys | **Navigate** | Open maps and use saved locations or routes. |
| Capture | **Capture Control** | Manage a capture already in progress. |
| Capture | **Video** | Record what you see as a video. |
| Capture | **Dive Trace** | Save camera movement for replay or analysis. |
| Capture | **Cave Slice** | Save part of a cave as a new pre-compiled map. |
| Troubleshooting | **Log Information** | Configure the amount of information in the logs. The new setting takes effect after a restart. |
| Troubleshooting | **Application Logs** | Open the latest log when you need help diagnosing a problem. |
| Troubleshooting | **Last Error** | Review and copy details from the latest recorded error. |

Shortcut rows retain one stable keycap lane and one flexible action lane.
Keycaps use 4-logical-pixel corners while preserving the geometric unit spans
defined above. At compact widths, the action lane moves below the keycap lane
before either can clip. Supporting artifact text follows the action label and
remains visually subordinate.

All Help cards have a one-logical-pixel `#30343D` border. Reserve the final
canvas device pixel so the right outline remains visible on every tab. **Log
Information** comes first and uses a 44-logical-pixel-high rounded choice
control whose width matches **Show latest log** via the shared action width
(140 logical pixels). The menu matches the trigger width. Present the
control with a 4-pixel radius, 15-pixel semibold text, a muted border, and an
amber `#F5C451` chevron that points upward while open. Its app-owned menu sits
4 logical pixels below the trigger, with two 40-pixel rows, 13-pixel semibold
labels, `#15171C` fill and a one-pixel `#30343D` border. Highlight the active
row with `#1F2228`; mark the saved choice with an amber check independently
of hover or keyboard navigation. Its options are **Essential** (default) and
**All**. Keep the restart guidance visible; show a failed save beneath the
control while retaining the previous selection.

**Show latest log** and **Copy** use the same 4-logical-pixel rounded action
treatment as Preferences. They preserve visible focus, Return and Space
activation, disabled state, and stable labels. The last-error excerpt uses a
4-logical-pixel rounded semantic container. Copy success uses the shared
transient confirmation mark; failure remains explicit text. Width, tab, and
troubleshooting-state changes recompute wrapped content, card height, the scroll
region, and scrollbar visibility without leaking canvas items or callbacks.

## Feedback lifetimes

Use the semantic constants in `caveviewer.gui.tk_feedback` instead of numeric
timeouts: success confirmations last 4 seconds, informational statuses 5,
warnings 7, recoverable errors 9, and short copy confirmations 2. A newer
action or leaving the owning surface clears transient feedback early.

Progress messages remain until their operation advances, completes, fails, or
is cancelled. Actionable validation and persistence errors remain until the
user corrects the value, retries, dismisses the surface, or replaces the
message; they must not be converted into expiring feedback merely to reuse a
timeout constant.

## Navigation rail and application status

The sidebar is flush with the window's left, top, and bottom edges. It is 220
logical pixels wide with square corners, a `#15171C` fill, and one `#30343D`
right divider. Its content uses 16-pixel horizontal and bottom insets and a
64-pixel top inset. Apply the active display scale once to every measurement.

Navigation entries are 188 by 44 logical pixels with eight pixels between
them. Their 18-pixel user-supplied icons and labels share the same state color:
`#F5C451` for the active destination and `#EFF1F5` for inactive destinations.
Every entry remains transparent. Labels use 13 logical pixels, regular when
inactive and semibold when active. The full row is pointer-activatable while
the label remains the keyboard focus owner.

The navigation rail deliberately has no repeated app name or logo: native
window chrome already identifies the application, while a quiet rail keeps
attention on navigation. Start navigation near the top with no decorative
masthead.

The lower application-status block is pinned after flexible sidebar space and
does not repeat the installed version; About owns that product detail. When an
update has a meaningful state, show one centered 13-pixel medium action or
status row in muted `#A9AFBC`; omit the block entirely when there is no update
state to communicate. Keep it within the 188-pixel content column without a
card, pill, border, or divider.

All actionable text links use the native hand cursor through the shared
`text_link.configure_text_link` configuration. This includes About websites,
cave source and back links, and update actions. Non-actionable text inherits
the default cursor. Link activation remains available by mouse, Enter, and Space.
For an available update, use one label: **Update to version &lt;version&gt;**. If
the update source omits its version unexpectedly, fall back to **Update** rather
than presenting an incomplete sentence. Reserve the thin progress lane and its
spacing as soon as the meaningful update block appears. The inactive lane is
blank; downloading and verification change only its drawing, never the footer's
geometry. Active transfer and verification expose a separate compact
**Cancel** link; verification is indeterminate and does not imply completion.
A completed download uses one label: show **Update ready** for three seconds,
then replace it with the amber platform-native reveal link: **Show in Finder**,
**Show in Explorer**, or **Open Download Folder**. The link reveals the already
verified package; it does not install or execute it.

## Viewer controls

The viewer exposes one documented keyboard command for closing its window:
**Esc**. Do not present a platform-specific `Ctrl/Cmd + W` close shortcut in
the controls overlay. If a capture is active, Escape first discards it: show
**Canceling…** during cleanup, then keep the artifact-specific no-save result
visible for three seconds before closing the viewer. A native window-close
request is intentionally different: it preserves the active artifact and uses
the **Finishing…** save-on-close treatment below.

## Viewer loading and capture feedback

The OpenGL viewer uses the same primary/supporting hierarchy while respecting
its own bitmap-font rendering scale:

- **Primary message** is the largest light label and explains the current
  stage, such as **Prepare to record a dive** or **Saving video**.
- **Supporting message** is muted and gives the next useful detail, such as
  the keyboard shortcut, save guidance, or the location-opening notice.

For video recording and dive tracing, the circular countdown/status indicator
comes first, followed by the primary message and then the supporting message.
All three are centered on the same axis. Do not add a separate feature label:
the primary message already identifies the action, and a second label is
redundant.

If the viewer is closed while a video or dive trace is still being written,
keep the window open and replace the cave view with the same centered status
treatment. Use **Finishing video** or **Finishing dive trace** as the primary
message and explain that CaveViewer will close automatically once the file is
saved. Leave the status visible briefly even when the writer finishes
immediately. Do not open a file browser during this exit path, and ignore
repeat close requests until the writer has finished.

Routine viewer waits use the shared flat progress bar: full-screen import and
initial streaming keep their stage hierarchy, while minimap and bookmark
repositioning use centered **Jumping to the selected point** text and the same
300-by-4 logical-pixel bar. The bar midpoint is anchored at the usable
viewport's horizontal and vertical center. Every routine indicator measures the
visible title bottom to bar top at 40 logical pixels and the bar bottom to an
optional description at 30 logical pixels; omitting a description collapses
only that optional space, never the title or bar position.
The initial Tk launch surface uses the same measured layout and control scale,
with the live two-tone product wordmark above the bar and the supplied subdued
cave mesh behind it. The mesh covers the viewport without stretching and fades
to the application surface at the center. Initial streaming and first-time map
building retain their solid background. Renderer-specific font technology
remains separate: Tk uses negative logical-pixel font tuples from the
process-registered Inter family and OpenGL rasterizes the bundled Inter file in
renderer logical units. Neither path uses Tk point sizes for application-owned
text.
For map opening, source import/cache construction and initial streaming are
one user-facing progress session: keep the bar continuous and retain the
current stage as the primary message. Do not add a separate operation title;
reset only for a new or terminally abandoned open. The bar remains non-numeric;
any bounded phase milestone is based on the existing measured work state rather
than an elapsed-time estimate.
For a first-time source import, the supporting message is one stable, concise
centered line for the full session; it does not change when the stage reaches
initial streaming. A cache-only opening uses no routine supporting message,
because `Preparing cave…` already identifies its brief work.
Capture countdown/status feedback uses a standard vector circle with the
remaining seconds or status symbol centered inside it. The circle uses the same
semantic fill/track colors and four-logical-pixel thickness as the flat loading
bar, while remaining distinct from routine loading by shape and context. Viewer
overlay text continues to follow `CAVEVIEWER_UI_TEXT_SCALE`; do not substitute
raw Tk font sizes or a second platform multiplier.

Map Library rows reserve a three-logical-pixel progress lane with five logical
pixels of preceding space. The lane is blank while idle and draws its track and
fill only while active. Download and rebuild state changes never add or remove
the lane, so neighboring rows remain fixed.

## Applying the system

1. Use `create_tk_typography(font_family, semibold_family=...,
   semibold_styles=..., text_scale=..., display_scale=...)` to obtain the
   semantic font roles. Standalone Tk surfaces obtain `text_scale` through
   `resolve_tk_text_scale(root, profile)` and `display_scale` from the resolved
   display metrics; the retained shell passes its existing scale snapshot.
2. Pass the relevant roles into a panel's style object or constructor.
3. Reuse the role appropriate to the text's meaning. Avoid raw `(family,
   size)` tuples in presentation components.
4. When larger text changes density, adjust spacing before reducing text back
   below its role's defined size.

The OpenGL viewer overlay has a separate rendering and accessibility scale,
but its primary and supporting roles intentionally match this system.
