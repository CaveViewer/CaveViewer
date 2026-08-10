# Design system

This document defines the small, shared visual system for CaveViewer-owned
interfaces. It currently applies to the Tk startup shell, Map Library, cave
details, About, and Preferences. Native operating-system window chrome is not
part of this system; the platform owns its title-bar typography.

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

For an available update, the subsection is simply **Download update**. Reserve
progress, verification, failure, and completed-download messages for the
states that follow the user's action. A completed download uses one label:
show **Update ready** for three seconds, then replace it with the amber
**Show update** link. The link reveals the already verified package; it does
not install or execute it.

## Applying the system

1. Use `create_tk_typography(font_family, text_scale=...)` to obtain the
   semantic font roles.
2. Pass the relevant roles into a panel's style object or constructor.
3. Reuse the role appropriate to the text's meaning. Avoid raw `(family,
   size)` tuples in presentation components.
4. When larger text changes density, adjust spacing before reducing text back
   below its role's defined size.

The OpenGL viewer overlay has a separate rendering and accessibility scale.
It should adopt equivalent semantic roles in a dedicated follow-up rather
than silently mixing bitmap-font scale factors with this Tk system.
