# Design system

This document defines the small, shared visual system for CaveViewer-owned
interfaces. It applies to the Tk startup shell, Map Library, cave details,
About, Preferences, and the OpenGL viewer's loading and capture feedback.
Native operating-system window chrome is not part of this system; the platform
owns its title-bar typography.

## Typography

`src/caveviewer/gui/tk_typography.py` is the canonical source for Tk text
roles. Components receive those roles through their presentation style or
constructor rather than choosing a font size for an individual widget.

| Role | Base Tk size | Weight | Use |
| --- | ---: | --- | --- |
| `display` | 18 | bold | One primary page subject, such as a cave name. |
| `heading` | 14 | bold | App identity and top-level panel headings. |
| `body_strong` | 10 | bold | Primary actions, selected navigation, map titles, and key values. |
| `body` | 10 | regular | Navigation, prose, editable controls, links, and menu actions. |
| `supporting` | 9 | regular | Locations, descriptions, version text, hints, statuses, and disclaimers. |
| `section` | 9 | bold | Section labels such as **CaveViewer Maps**, **Key facts**, and **Source**. |

The base values are logical Tk points, not fixed screen pixels. CaveViewer
applies the active platform accessibility scale once when it creates a
`TkTypography` instance. Components must not add a second multiplier or use
platform-specific replacements for these roles.

On Linux, Tk's own DPI scaling determines the physical size of point fonts.
The semantic type system therefore does not add a second multiplier based on
the desktop's `TkDefaultFont` size.

On Windows, keep Tk point scaling and logical-pixel geometry as separate native
conversions. Tk initializes its pixels-per-point value for fonts; CaveViewer
does not rewrite it during normal startup. Pixel geometry uses the effective
window DPI divided by 96 exactly once. The shell then applies the same bounded
physical-density factor to semantic fonts and logical geometry: monitors up to
24 inches retain `1.00`, larger monitors use `clamp(24 / diagonal_inches,
0.875, 1.00)`, and invalid or unavailable raw measurements retain `1.00`. This
keeps monitors of approximately 27.4 inches and larger at the same modest
density floor. On a settled Windows monitor transition, CaveViewer recomputes
the factor and rebuilds the
retained Tk shell off-screen so semantic fonts and logical geometry adopt the
destination scale together. The current outer window size changes by the same
scale ratio. Normal windows retain at least the destination's 1040-by-740
logical-pixel default when the work area permits, larger user sizing is
preserved proportionally, and maximized windows remain maximized. All resulting
bounds are clamped to the destination work area. Tk 8.6 retains one
pixels-per-point value on the process-owned root, so monitor recomposition also
synchronizes that value to the destination's native DPI divided by 72 before
creating replacement widgets. This is the font conversion paired with, not an
additional multiplier on, the independent DPI-divided-by-96 geometry scale.

The development-only `CAVEVIEWER_TK_SCALE` override is expressed in Tk pixels
per point and is the only path that deliberately replaces Tk's native value;
while active, it also bypasses adaptive density. CaveViewer does not expose a
persisted UI-size preference. macOS, Linux, and OpenGL surfaces do not use the
Windows physical-density factor.

### Role mapping

- Map Library rows use `body_strong` for map names and `supporting` for their
  location, availability, and cache status. Map names use all width remaining
  after fixed trailing columns for size, primary action, and overflow action;
  they wrap only when the live window width truly requires it, rather than at a
  fixed column width. Text never displaces or clips those controls.
- The **Open a local map** card uses `body_strong` for its action and
  `supporting` for its explanation.
- Navigation uses `body`, switching only the active item to `body_strong`.
- Cave details use `display` only for the cave name, `body` for facts and
  sources, `body_strong` for statistic values, `supporting` for its location
  and disclaimer, and `section` for section labels.
- Preferences use `body` for fields and tabs, `body_strong` for buttons, and
  `supporting` for field hints and feedback.
- Preferences and Help render related groups with the standard section
  spacing: an uppercase `section` label, 13 logical pixels before its first
  content row, and 26 logical pixels before the next group. Use the active
  UI scaling helper or spacing token rather than device-pixel literals. Do not
  add heading rules, cards, amber section decoration, or shortcut-row rules in
  Help; use whitespace to separate its rows.

Text hierarchy should come first from role, then from color and spacing.
Do not create a new font size merely to distinguish a control state; use the
appropriate weight, color, or interaction treatment instead.

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

The primary shell uses a compact native desktop baseline: a 190-logical-pixel
navigation rail, 24 logical pixels between the rail and content, 24-pixel
navigation icons, and compact row padding. Map Library actions use stable
50-pixel feature rows and 28-pixel row controls. These values are logical units;
the Windows shell's combined native-DPI and bounded-density layout scale is
still applied exactly once.

Map Library, Preferences, and Help share a 14-logical-pixel outer vertical
margin and a 12-logical-pixel primary left edge inside the right-hand surface.
Tabbed surfaces use asymmetric outer padding: no additional left inset and the
platform profile's existing gutter on the right. Tab text, section headings,
and first content columns align to the shared left edge, so navigation never
moves the primary content origin.

The navigation rail deliberately has no repeated app name or logo: native
window chrome already identifies the application, while a quiet rail keeps
attention on navigation. Start navigation near the top with no decorative
masthead.

The lower-left application-status block does not repeat the installed version;
About owns that product detail. When an update has a meaningful state, show one
compact amber `supporting`-role action or status row; omit the block entirely
when there is no update state to communicate. This footer action is a secondary
link, not the primary action of the active panel. Keep the block left-aligned,
without a card or divider.

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
300-by-4 logical-pixel bar. Map-loading and jump labels share the same bitmap
type size, responsive scale, chalk color, and 60-logical-pixel label offset.
The initial Tk launch surface uses its semantic heading role at the same
60-logical-pixel offset, while launch, initial streaming, and first-time map
building share the solid Void background. Renderer-specific font technology
remains separate: Tk uses the platform family and OpenGL uses its scaled bitmap
renderer.
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

1. Use `create_tk_typography(font_family, text_scale=...)` to obtain the
   semantic font roles.
2. Pass the relevant roles into a panel's style object or constructor.
3. Reuse the role appropriate to the text's meaning. Avoid raw `(family,
   size)` tuples in presentation components.
4. When larger text changes density, adjust spacing before reducing text back
   below its role's defined size.

The OpenGL viewer overlay has a separate rendering and accessibility scale,
but its primary and supporting roles intentionally match this system.
