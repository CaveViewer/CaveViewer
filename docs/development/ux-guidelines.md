# UX guidelines

This document defines the current interaction and experience standards for
CaveViewer-owned desktop interfaces. It applies to the startup and library
shell, Map Library, Preferences, Help, application dialogs, and the viewer's
loading and status presentations.

Brand identity is deliberately out of scope. Do not use this document to
choose or evaluate logos, application icons, artwork, brand colors, packaging
assets, or other branded artifacts. Those concerns belong in
[branding.md](branding.md). Shared typography roles and low-level component
primitives remain documented in [design-system.md](design-system.md).

## Experience principles

- Keep the primary task obvious. Avoid duplicate titles, labels, icons, and
  status text when window chrome or the active surface already supplies the
  same information.
- Prefer concise, specific language over implementation terminology. Describe
  what the user can do or what the application is doing.
- Show feedback only when it changes a decision, explains a delay, confirms a
  completed user action, or gives a recovery path.
- Keep ordinary successful behavior quiet. Do not expose capability probes,
  fallback routes, compatibility layers, or other internal diagnostics when an
  action completes normally.
- Preserve user work. Destructive, replacing, or unsaved-state transitions
  require an explicit decision and a safe cancellation path.
- Use the event loop for visible waits and transitions. Never block the UI
  thread to create a timing effect.

## Navigation and surface ownership

- Use one persistent application shell rather than opening independent windows
  for primary sections.
- Keep navigation stable while changing the active content surface. The active
  item uses semantic emphasis and focus state; it does not require a decorative
  card or repeated application identity.
- Build deferred surfaces before exposing them when the first interactive frame
  depends on their geometry or state. Do not reveal partially composed panels.
- When leaving a surface with unsaved edits, keep that surface active until the
  user chooses to keep, discard, or save the changes.
- Return keyboard focus to a stable, meaningful target whenever navigation
  changes the active surface.

## Layout, density, and scaling

- Express layout dimensions in logical units and apply the active display-scale
  helper exactly once. Do not tune against device pixels from one screenshot.
- Respect the operating system's effective display scale. Do not add a
  user-facing CaveViewer UI-size preference or replace the platform DPI value.
- Two displays with the same pixel resolution and operating-system scale receive
  the same native geometry scale. On Windows, the Tk shell additionally applies
  one bounded density factor on monitors larger than 24 inches so typography,
  controls, icons, spacing, and window geometry remain proportional. Invalid or
  unavailable physical-monitor data preserves full density. Bound the physical
  adjustment to `clamp(24 / diagonal_inches, 0.95, 1.00)` so monitors around
  25.3 inches and larger retain a modest, readable common floor.
- Recompute Windows adaptive density after a monitor move settles; do not resize
  continuously while the pointer is dragging. Rebuild the retained Tk shell
  off-screen, preserve its active surface and unsaved Preferences values, and
  scale the last settled source-monitor normal bounds once by the
  destination/source ratio. Use Windows' settled destination position without
  treating its already DPI-adjusted dimensions as source bounds. Explicit
  development scaling overrides bypass adaptation so one override remains
  authoritative. Carry the settled destination measurements into the rebuild
  rather than remeasuring a withdrawn root, synchronize Tk point scaling before
  replacement widgets are created, and detach the prior composition's root
  observer so one move produces one lifecycle transition. For a normal window,
  keep the larger of its proportionally scaled bounds and the destination's
  1040-by-740 logical-pixel default, then clamp to the destination work area;
  preserve a maximized window as maximized and retain its transformed normal
  restore bounds.
- Use semantic typography roles from `caveviewer.gui.tk_typography`; do not
  introduce a new font size to solve a spacing problem.
- Separate related content with the shared section-spacing tokens. Prefer
  whitespace and alignment over decorative containers or divider lines.
- Align repeated labels, fields, actions, and descriptions to stable columns.
  Do not let label length move the beginning of adjacent content.
- Give tabbed content one primary left edge shared by the tab label, section
  headings, and the first content column. Use an additional inset only when it
  communicates real hierarchy consistently across the surface.
- Keep ordinary stacked shell surfaces on one stable origin. Map Library,
  Preferences, and Help use the same 14-logical-pixel vertical surface margin
  and 12-logical-pixel primary left edge; retain platform-specific right
  gutters without mirroring them onto the left.
- Size normal desktop windows for their complete primary content, then clamp
  them to the usable display area. Scrolling is the fallback for compact
  displays and accessibility scaling, not the default presentation on an
  ordinary desktop. Before first reveal, a normal main shell stages Preferences
  at its rendered stacked-surface width, measures and verifies its tallest tab,
  then restores the intended surface while preserving a larger restored height.
  On a settled monitor recomposition, first map the retained root invisibly on
  the destination display and reject stale child geometry whose viewport is
  larger than the actual root before applying the same fit. Restore the intended
  surface before reveal; unbounded Map Library lists remain scrollable.
- Use the same compact primary-shell geometry on Windows, macOS, and Linux:
  1040 by 740 logical pixels by default and an 840 by 600 logical-pixel resize
  minimum.
  Clamp both the initial geometry and minimum constraints when the available
  display is smaller. Establish native resize capability before the window is
  first shown; do not toggle native frame styles during a visible surface
  handoff.
- Recalculate wrapping and overflow from the final mapped width. Coalesce idle
  layout work and avoid update loops that repeatedly measure unchanged geometry.
- In Map Library rows, give the text column the remaining width and reserve
  trailing columns for size, primary action, and overflow action. Long names
  may wrap, but they must never displace or clip row controls.

## Actions and labels

- Label buttons with a concise verb that describes their result: **Save**,
  **Discard**, **Restore**, **Cancel**, **Close**, **Browse**, or **Copy**.
- Do not repeat the dialog title or surrounding noun phrase in every action.
  For example, a dialog titled **Restore default preferences?** uses
  **Cancel** and **Restore**, not **Keep current values** and
  **Restore defaults**.
- Use **Cancel** for a two-action confirmation's safe exit. A three-way
  unsaved-changes decision uses **Keep**, **Discard**, and **Save** because all
  three choices describe distinct outcomes.
- Present one clear primary action. Secondary actions must remain visually and
  semantically available without competing with it.
- Disabled actions must explain why they are unavailable when the reason is not
  already evident. Never leave a required action looking interactive when it
  cannot execute.
- Keep action hit targets consistent within a surface. Text fields and adjacent
  actions in one control row use matching height.

## Forms and Preferences

- Give every field a short label and a supporting explanation. Labels state the
  setting; hints describe its effect rather than repeating the label.
- Group fields by user goal and place related groups under semantic section
  labels. Tabs divide major preference domains; they do not replace grouping
  inside a dense page.
- Keep numeric inputs compact and path inputs wide enough to recognize the
  selected location.
- Present a directory path and its **Browse** action as one compound control:
  one outer border, one internal seam, matching height, and one shared focus or
  invalid-state treatment. The read-only path remains text; the action retains
  its full keyboard-focusable target.
- Preserve pending values while moving between tabs. Show dirty state without
  replacing labels or field contents with transient status text.
- Keep **Save** and **Discard** available in a stable action area. When vertical
  space is constrained, form content scrolls before the action area is clipped.
- Validation identifies the affected field, moves to its tab when necessary,
  and retains the message until the user corrects or replaces it.

## Dialogs and confirmations

- Use CaveViewer-owned modal dialogs for application confirmations and
  messages. Do not introduce native Tk message boxes alongside the app-styled
  dialog system.
- A modal is owned by and centered over its parent, blocks interaction with
  that parent, inherits its application window treatment, and releases its grab
  when closed.
- **Escape** and the window close control choose the safe/cancel outcome.
  Keyboard focus starts on the primary action, and every action supports the
  established keyboard activation keys.
- Ordinary confirmation and message dialogs use a 430 by 220 logical-pixel
  minimum, 28 logical pixels of horizontal content inset, and 24 logical pixels
  of vertical content inset. Content may increase the requested size when text
  or accessibility scaling requires it.
- Anchor the action row to the bottom content inset. This preserves a clear gap
  after the message without leaving arbitrary empty space below the buttons.
- Right-align actions and keep an 8-logical-pixel gap between them.
- Use one **Close** action for informational, warning, and error messages.
  Two-action confirmations use **Cancel** and an action-specific verb.
  Unsaved Preferences uses **Keep**, **Discard**, and **Save**.
- Keep confirmation copy explicit about what changes and whether the result is
  saved immediately. Do not rely on button labels to carry the complete risk
  explanation.

## Feedback and errors

- Reserve persistent feedback areas for actionable validation, unavailable
  functionality, recoverable failures, and meaningful completion messages.
- Do not show successful preflight or compatibility notices. If a platform
  picker or fallback works normally, open it without adding a status label.
- Translate implementation exceptions into concise user-facing language at the
  UI boundary. Put diagnostic detail in the application log, never in the main
  message unless it helps the user recover.
- Treat a failed first-map import as a recoverable viewer-session outcome.
  Close the empty viewer and restore the Map Library so Help, Troubleshooting,
  and retry paths remain available; do not terminate the GUI application.
- After the Map Library is visibly restored, present the failure with the
  standard app-owned two-action modal. Use generic user-facing copy for an
  edge-case failure, **Copy details** for the underlying diagnostic bundle,
  and **Dismiss** to close; copying must not dismiss the modal.
- Keep action labels stable after activation. Confirm a successful clipboard
  action with an adjacent geometric check mark that disappears after three
  seconds; keep failures visible as explicit adjacent text rather than
  renaming the action control.
- Do not communicate severity through color alone. Error dialogs pair their
  heading and error color with a recognizable semantic icon whose shape remains
  meaningful without color perception.
- Use the same semantic heading structure for two- and three-action dialogs:
  a deterministic error, warning, or information icon followed by the title on
  the first row. Begin description text on the next row at the icon’s left edge,
  not indented beneath the title.
- When a replacement main shell is already composed and ready after a native
  viewer returns, reveal it before entering its event loop. Do not leave the
  first mapping of a withdrawn recovery window dependent on an idle callback.
- Treat the Tk application root as process-owned on Windows and macOS. Withdraw
  it while the native viewer owns the foreground, then rebuild and reveal the
  Map Library on that same root rather than creating another Tk interpreter.
- Keep progress feedback until the operation advances, completes, fails, or is
  cancelled. Keep validation and persistence errors until corrected, retried,
  dismissed, or replaced.
- Use the feedback lifetimes defined in `caveviewer.gui.tk_feedback` for
  transient confirmations and statuses. A newer action or leaving the owning
  surface clears obsolete transient feedback.
- Do not use modal dialogs for routine success when inline or temporary status
  feedback is sufficient.

## Progress and waiting

- Prefer a flat progress bar for routine determinate and indeterminate work.
  Place it below the primary stage label and above the supporting description.
- Use that same geometry for launch, viewer import and streaming, repositioning,
  Map Library transfers and cache work, and update transfer or verification.
  Full-surface launch and map-loading states also share the branded Void
  background; do not place separate startup artwork behind routine progress.
  Keep stop, pause, and cancel as separate focusable controls beside the stage;
  do not wrap those glyphs in circular progress geometry.
- Within the OpenGL viewer, map loading and jump feedback share the same bitmap
  type size, responsive scale, label color, bar dimensions, and spacing. Keep
  jump copy ASCII-safe for the bitmap-font renderer.
- Reserve the Map Library row's progress lane when the row is created. An
  inactive lane is visually empty, but starting or ending work must not add,
  remove, or resize row content.
- Determinate progress is monotonic. Indeterminate progress uses visible motion
  without implying a percentage.
- A completion frame may be shown briefly so the user can perceive the result;
  do not add a blocking sleep to hold it.
- Startup remains visible until both its minimum presentation interval and the
  readiness of the first interactive application frame are satisfied. Slow
  initialization stays visible beyond the minimum; fast initialization uses
  scheduled event-loop updates rather than artificial blocking.
- Startup progress blends real milestones with smooth visual advancement,
  remains below completion while work or the minimum interval is outstanding,
  and reaches completion only when the transition may proceed.
- Loading and progress presentation must not hide cancellation, failure, or
  shutdown state. Long-running work exposes an explicit state model rather than
  relying on changing labels alone.
- Circular capture countdowns are task feedback rather than routine loading.
  Draw a standard vector circle with the remaining seconds centered inside it;
  use the same semantic colors and stroke thickness as the shared flat bar.

## Keyboard and accessibility

- Every essential pointer action has a keyboard path. Interactive labels and
  custom-drawn actions must opt into focus and bind the established activation
  keys.
- Focus is always visible. Compound controls show focus around the entire
  interactive unit; invalid styling takes precedence over ordinary focus color.
- **Escape** closes or cancels the current modal operation unless a workflow
  explicitly documents a safer cleanup sequence.
- Use the platform presentation profile for modifier names, font selection,
  display scaling, and platform-specific input conventions.
- Do not encode state through color alone. Combine it with type weight, text,
  geometry, enabled state, or another perceivable treatment.
- Preserve readable wrapping and complete actions at supported accessibility
  scales. When content cannot fit, provide scrolling rather than clipping.

## Platform integration

- Use the composed desktop service for file and directory selection, file
  reveal, notifications, and other operating-system actions. Probe capability
  at action time when availability can change.
- Prefer the platform's appropriate picker route, but keep route selection and
  fallback details out of normal user-facing copy.
- Keep operating-system window chrome native. Application-owned content inside
  a modal or panel follows CaveViewer's shared interaction system.
- Parent every picker and modal to the active application window so focus,
  stacking, modality, and task switching behave predictably.
- Fail closed when an essential platform action has no safe route. Explain the
  unavailable action and provide a recovery path when one exists.

## Responsiveness and lifecycle

- Tk and OpenGL work stays on its owning thread. Long-running I/O, parsing,
  encoding, and cache work runs outside the UI thread and reports state through
  bounded handoff points.
- Keep callbacks short. Schedule later work through the event loop and cancel
  owned callbacks when their surface is destroyed.
- Model loading, success, failure, cancellation, retry, and shutdown explicitly.
  Repeated cancellation and close requests must be safe.
- Preserve the last valid user data or cache on failure. Remove partial output
  and keep failure cleanup deterministic.
- Do not reveal a destination or dismiss a progress surface before publication
  and UI readiness are complete.

## UX validation checklist

For a user-visible change:

1. Exercise the primary pointer and keyboard paths.
2. Verify focus order, visible focus, Escape behavior, and window-close behavior.
3. Test success, cancellation, unavailable capability, recoverable failure, and
   destroyed-window cleanup where applicable.
4. Review the normal display scale and at least one larger accessibility scale.
5. Review an ordinary desktop size and a compact usable screen area; confirm
   that content scrolls instead of clipping.
6. Confirm that transient feedback clears and persistent errors remain for an
   actionable lifetime.
7. Confirm that the UI exposes no internal route, probe, exception, or debug
   terminology during normal operation.
8. Run focused GUI tests, the complete test suite when practical, syntax checks,
   and `git diff --check`.

Automated tests should protect policy and state transitions without depending
on a real display when a pure controller, layout token, or presentation contract
can express the behavior. Native Windows, macOS, Ubuntu, and Fedora smoke checks
remain necessary for window management, scaling, pickers, focus, and platform
integration that Tk fakes cannot reproduce faithfully.

UI screenshot concepts must start from a current CaveViewer screenshot supplied
for the affected surface. Ask for a baseline when none is available; do not
invent unrelated navigation, content, or surrounding application structure.
