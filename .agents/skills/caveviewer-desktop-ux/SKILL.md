---
name: caveviewer-desktop-ux
description: "Review, design, and implement CaveViewer desktop UX across Tk and OpenGL surfaces. Use for Preferences, Help, dialogs, progress, loading, viewer overlays, layout, scaling, typography, feedback, or accessibility; not for standalone brand artwork or packaging."
---

# CaveViewer desktop UX

Apply CaveViewer's shared interaction and presentation system across Windows,
macOS, Ubuntu, and Fedora without inventing a second component language.

## Start from the current surface

1. Read `docs/development/ux-guidelines.md` and
   `docs/development/design-system.md` completely.
2. Read `docs/development/branding.md` only when the request changes branded
   artwork or brand color roles. Branding and ordinary layout are separate
   decisions.
3. Inspect the applicable scoped `AGENTS.md`, current component, shared style or
   controller, and focused tests before proposing a change.
4. For a screenshot concept, require a current CaveViewer screenshot for the
   affected surface. Preserve its functionality, navigation, and content.

## Make the change

- Use semantic typography from `caveviewer.gui.tk_typography` and shared spacing
  or presentation tokens. Express geometry in logical units and apply display
  scaling exactly once.
- Prefer stable alignment, whitespace, and hierarchy. Do not add a font size,
  divider, card, title, icon, or status merely to fill space.
- Preserve the established app-owned modal system, concise action labels,
  keyboard focus, Escape behavior, parent ownership, and semantic message
  icons. Never communicate severity or state through color alone.
- Keep successful routine behavior quiet. Use adjacent, time-bounded feedback
  for actions such as Copy; use dirty state and disabled Save state for
  persistence rather than a redundant success check mark.
- Model loading, success, failure, cancellation, retry, and shutdown explicitly.
  Keep long work off the UI thread, callbacks short, and all timers or callbacks
  owned and cancelled with their surface.
- Compose deferred content before revealing it. Do not expose partially laid
  out panels or create additional Tk roots during viewer-to-library recovery.

## Verify the experience

Run the focused tests matching the affected surface, commonly under
`tests/unit/gui/`, then the repository's standard validation. Prefer pure
controller, token, and presentation-contract tests over display-dependent
assertions.

Manually exercise pointer and keyboard paths, focus order, Escape and native
close behavior, success and failure cleanup, ordinary and compact window sizes,
normal and increased accessibility scale, and every affected operating system.
Report native platform checks that remain.
