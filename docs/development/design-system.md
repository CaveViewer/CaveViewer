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
| `display` | 20 | bold | One primary page subject, such as a cave name. |
| `heading` | 16 | bold | App identity and top-level panel headings. |
| `body_strong` | 12 | bold | Primary actions, selected navigation, map titles, and key values. |
| `body` | 12 | regular | Navigation, prose, editable controls, links, and menu actions. |
| `supporting` | 10 | regular | Locations, descriptions, version text, hints, statuses, and disclaimers. |
| `section` | 10 | bold | Section labels such as **CaveViewer Maps**, **Key facts**, and **Source**. |

The base values are logical Tk points, not fixed screen pixels. CaveViewer
applies the active platform accessibility scale once when it creates a
`TkTypography` instance. Components must not add a second multiplier or use
platform-specific replacements for these roles.

On Linux, Tk's own DPI scaling determines the physical size of point fonts.
The semantic type system therefore does not add a second multiplier based on
the desktop's `TkDefaultFont` size.

### Role mapping

- Map Library rows use `body_strong` for map names and `supporting` for their
  location, availability, and cache status. Map names use all width remaining
  after their action controls; they wrap only when the live window width truly
  requires it, rather than at a fixed column width.
- The **Open a local map** card uses `body_strong` for its action and
  `supporting` for its explanation.
- Navigation uses `body`, switching only the active item to `body_strong`.
- Cave details use `display` only for the cave name, `body` for facts and
  sources, `body_strong` for statistic values, `supporting` for its location
  and disclaimer, and `section` for section labels.
- Preferences use `body` for fields and tabs, `body_strong` for buttons, and
  `supporting` for field hints and feedback.
- Preferences and Help render related groups with the standard section
  spacing: an uppercase `section` label, 16 logical pixels before its first
  content row, and 28–32 logical pixels before the next group. Use the active
  UI scaling helper or spacing token rather than device-pixel literals. Do not
  add heading rules, cards, amber section decoration, or shortcut-row rules in
  Help; use whitespace to separate its rows.

Text hierarchy should come first from role, then from color and spacing.
Do not create a new font size merely to distinguish a control state; use the
appropriate weight, color, or interaction treatment instead.

## Navigation rail and application status

The navigation rail deliberately has no repeated app name or logo: native
window chrome already identifies the application, while a quiet rail keeps
attention on navigation. Start navigation near the top with no decorative
masthead.

The lower-left application-status block shows the version in `supporting`
text. When an update has a meaningful state, show a compact status subsection
below the version and its amber `supporting`-role action; omit that subsection
entirely when there is no update state to communicate. This footer action is a
secondary link, not the primary action of the active panel. Keep this block
left-aligned, without a card or divider. The more detailed transparent app
mark remains for the larger About presentation.

For an available update, the subsection is **Update to &lt;version&gt;**. Reserve
progress, verification, failure, and completed-download messages for the
states that follow the user's action; active transfer and verification expose a
compact **Cancel** link. A completed download uses one label: show **Update
ready** for three seconds, then replace it with the amber platform-native
reveal link: **Show in Finder**, **Show in Explorer**, or **Open Download
Folder**. The link reveals the already verified package; it does not install or
execute it.

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

The full-screen map-import panel keeps its import-specific title/ring/stage
arrangement, but uses the same amber, light, and muted roles. Viewer overlay
text continues to follow `CAVEVIEWER_UI_TEXT_SCALE`; do not substitute raw Tk
font sizes or a second platform multiplier.

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
