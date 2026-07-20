# AGENTS.md

## Purpose

This repository contains a Python Tk/Tkinter desktop application that must run correctly on macOS, Windows, and Linux.

All AI agents and contributors must preserve:

- Tkinter thread safety
- UI responsiveness
- Clear ownership of widgets and background tasks
- Separation of UI, application logic, and data access
- Cross-platform behavior
- Testability
- Predictable startup and shutdown behavior
- Compatibility with packaged application builds

Do not introduce platform-specific assumptions, blocking UI behavior, or unnecessary dependencies.

---

## Critical Rules

1. All Tk operations must occur on the main thread.
2. Long-running work must never execute directly in a UI callback.
3. Worker results must return through a thread-safe channel and be applied through `after()`.
4. The application must use exactly one `Tk()` root.
5. Additional windows must use `Toplevel`.
6. Every background task must define success, failure, cancellation, and shutdown behavior.
7. Business logic must remain testable without creating Tk widgets.
8. Do not call `sleep()`, perform blocking I/O, or synchronously wait on the UI thread.
9. Every change must preserve macOS, Windows, and Linux support.
10. Do not claim cross-platform compatibility without test or CI evidence.

---

## Tkinter Thread Safety

- Treat the Tk main thread as the only UI thread.
- Create the `Tk` root and all widgets on the main thread.
- Never call widget methods from worker threads.
- Never update `StringVar`, `IntVar`, `DoubleVar`, `BooleanVar`, or other Tk variables from worker threads.
- Use `after()` to schedule UI updates on the Tk event loop.
- Use a thread-safe queue, future, or equivalent mechanism to pass worker results to the UI thread.
- Poll worker queues through `after()` rather than blocking the UI.
- Keep event handlers and callbacks short.
- Do not call `time.sleep()` in callbacks.
- Do not perform network requests, database operations, large file reads, large file writes, subprocess waits, or expensive computation on the UI thread.
- Do not call blocking thread joins from the UI thread unless completion is guaranteed to be immediate.
- Do not allow worker threads to access destroyed widgets.
- Capture and report worker exceptions.
- Distinguish task success, failure, and cancellation explicitly.
- Prevent duplicate task launches when the same operation is already running.
- Provide cancellation for long-running operations where technically safe.

---

## Application Architecture

- Separate presentation, application logic, and data access.
- Do not place the entire application in one large `Tk` subclass.
- Organize major screens, dialogs, or panels as separate `Frame` or `Toplevel` classes.
- Keep business logic independent of Tkinter where practical.
- Keep file, network, database, and persistence logic outside widget classes.
- Prefer composition over deep widget inheritance.
- Pass only the dependencies a component actually requires.
- Avoid passing the root object throughout the entire application.
- Keep application-wide configuration in a dedicated configuration object or module.
- Store mutable application state in explicit model or controller objects rather than scattering it across widgets.
- Do not couple domain objects directly to widget instances.
- Avoid hidden global state.
- Keep platform-specific behavior behind a small abstraction boundary.
- Do not rewrite an existing architecture unless the task explicitly requires it.

---

## Widget Ownership and Lifecycle

- Every widget must have a clear owner responsible for creating and destroying it.
- Create only one `Tk()` instance.
- Use `Toplevel` for secondary windows.
- Do not mix `pack`, `grid`, and `place` within the same parent container.
- Prefer `grid` for structured application layouts.
- Configure row and column weights explicitly for resizable layouts.
- Avoid relying on implicit widget destruction.
- Cancel scheduled `after()` callbacks when their owning component is destroyed.
- Stop, cancel, or detach background workers during shutdown.
- Handle the main-window close protocol explicitly.
- Ensure dialogs and child windows do not retain stale references after closing.
- Release modal grabs when a dialog closes or fails.
- Associate child windows with the correct parent.
- Guard callbacks that may run after a widget has been destroyed.
- Avoid unnecessary use of `overrideredirect`.

---

## State Management

- Use Tk variables only for UI-facing state.
- Do not use Tk variables as the primary domain model.
- Keep model state in ordinary Python objects.
- Synchronize model state and UI state deliberately.
- Avoid uncontrolled bidirectional bindings.
- Use variable traces sparingly.
- Document trace side effects.
- Prevent recursive update loops caused by traces or event bindings.
- Validate user input before committing changes to application state.
- Distinguish temporary form state from committed state.
- Restore the UI to a consistent state after failed operations.
- Keep loading, success, failure, cancellation, and idle states explicit.

---

## Events and Callbacks

- Bind events at the narrowest practical scope.
- Avoid `bind_all()` unless behavior is truly application-wide.
- Do not overwrite unrelated event bindings.
- Use `add="+"` when multiple handlers must coexist.
- Prefer named callback methods over large lambdas.
- Use lambdas only for trivial argument forwarding.
- Avoid late-binding closure bugs when creating callbacks in loops.
- Keep business logic out of event handlers.
- Debounce high-frequency events such as resize, key release, search input, or text changes.
- Do not depend on undocumented Tk event behavior.
- Centralize shared input normalization such as mouse-wheel handling.

---

## Responsiveness and Progress

- Show progress for operations that are not effectively instantaneous.
- Use indeterminate progress when total work is unknown.
- Use determinate progress when measurable completion information is available.
- Do not update progress widgets excessively.
- Make busy state clear.
- Disable or guard controls that would trigger duplicate work.
- Provide cancellation where safe.
- Do not use nested event loops.
- Avoid repeated calls to `update()`.
- Use `update_idletasks()` only for narrowly justified layout refreshes.
- Do not use `update()` as a substitute for correct asynchronous design.

---

## Error Handling

- Do not silently suppress exceptions.
- Do not use broad `except Exception` blocks without logging or reporting.
- Route unexpected callback exceptions through a centralized error handler.
- Show concise, actionable messages to users.
- Keep technical details in logs rather than modal dialogs.
- Preserve exception context when re-raising.
- Validate external data before using or displaying it.
- Ensure failed operations leave the application in a valid state.
- Restore button state, cursor state, progress state, and other transient UI state after failures.
- Handle missing files, malformed files, incompatible configuration, unavailable external services, and permission failures gracefully.

---

## Layout and Visual Consistency

- Prefer `ttk` widgets over classic Tk widgets unless a classic widget is required.
- Use `ttk.Style` for visual customization.
- Do not assume widgets have identical dimensions, padding, fonts, or rendering across platforms.
- Do not depend on pixel-perfect placement.
- Allow layouts to expand based on requested widget size.
- Use consistent padding.
- Configure row and column weights for resizable areas.
- Set sensible minimum window sizes.
- Avoid fixed-size layouts unless explicitly required.
- Do not assume a fixed screen resolution, DPI, or scaling level.
- Test with long labels, large fonts, high-DPI scaling, and user-provided text.
- Avoid excessive modal dialogs.
- Preserve visible focus indicators.

---

## Accessibility and Usability

- Provide keyboard access for primary actions.
- Define sensible tab traversal.
- Associate labels clearly with controls.
- Do not communicate state using color alone.
- Preserve standard focus behavior.
- Use platform conventions for shortcuts.
- Provide meaningful button, menu, and dialog labels.
- Confirm destructive actions when consequences are difficult to reverse.
- Avoid trapping keyboard focus.
- Set initial focus deliberately when opening dialogs.
- Support Escape to cancel and Enter to confirm where appropriate.
- Ensure important functionality remains available without a mouse.
- Do not require hover or right-click for essential actions.

---

## Cross-Platform Requirements

The application must run on macOS, Windows, and Linux. Every change must preserve support for all three platforms.

### General Portability

- Do not assume the application is running on one operating system.
- Prefer Python standard-library APIs and Tk/Ttk abstractions over native shell commands.
- Isolate unavoidable platform-specific logic behind clearly named functions or modules.
- Use `sys.platform`, `platform.system()`, or capability detection only when necessary.
- Prefer feature detection over operating-system detection.
- Do not scatter operating-system checks throughout UI or business logic.
- Ensure unsupported platform branches fail gracefully with actionable errors.

### File Paths and Filesystem Behavior

- Use `pathlib.Path`.
- Do not manually concatenate paths with `/` or `\`.
- Do not assume drive letters, Unix roots, or a specific home-directory layout.
- Do not assume the current working directory is the project or application directory.
- Resolve packaged resources through a dedicated resource-location helper.
- Store configuration, logs, caches, and user data in appropriate per-user directories.
- Do not write runtime data into the source directory or installed application directory.
- Handle Unicode paths and filenames.
- Use explicit text encodings, normally UTF-8.
- Do not rely on platform-default encodings.
- Do not assume case-sensitive or case-insensitive filenames.
- Do not create files that differ only by letter case.
- Avoid characters invalid on Windows.
- Do not use reserved Windows names such as `CON`, `PRN`, `AUX`, `NUL`, `COM1`, or `LPT1`.
- Account for Windows file locking.
- Close files promptly.
- Use context managers.
- Use atomic replacement where partial writes could corrupt persistent data.
- Test paths containing spaces, Unicode characters, and long components.

### Environment and Subprocesses

- Do not assume a POSIX shell exists.
- Do not invoke `bash`, `sh`, PowerShell, Command Prompt, AppleScript, or platform utilities unless required.
- Avoid `shell=True`.
- Pass subprocess arguments as a sequence.
- Do not construct shell command strings from user input.
- Do not assume executable names or locations.
- Use `shutil.which()` or explicit configuration to find external programs.
- Handle executable suffixes and platform-specific process behavior.
- Do not rely on Unix signals for core behavior.
- Provide portable cancellation and termination behavior.
- Do not assume environment variables are identical across platforms.

### Tkinter Platform Behavior

- Prefer `ttk` widgets for native theme integration.
- Do not assume identical menu, focus, dialog, or window-manager behavior.
- Use Tkinter file dialogs and message dialogs unless a platform-specific implementation is explicitly required.
- Do not depend on undocumented behavior seen on only one OS.
- Do not use rendering workarounds based on repeated `update()` calls.
- Document any required `update_idletasks()` usage.
- Test against supported Tcl/Tk versions.
- Fail clearly if the installed Tk version lacks a required feature.

### Menus and Keyboard Shortcuts

- Follow platform conventions.
- Treat macOS Command and Windows/Linux Control as equivalent primary modifiers where appropriate.
- Do not hardcode Control shortcuts where macOS should use Command.
- Centralize shortcut definitions and modifier translation.
- Ensure actions remain accessible through menus or buttons.
- Account for platform differences in key names and event sequences.
- Do not override standard text-editing shortcuts.
- Ensure menu accelerators match actual bindings.
- Use a conventional macOS application-menu layout where practical.
- Do not assume right-click maps to the same event everywhere.

### Mouse and Scrolling

- Do not assume a multi-button mouse.
- Support trackpads and touchpads.
- Normalize mouse-wheel behavior in one place.
- Account for differing wheel events and delta values.
- Do not require right-click for essential functionality.
- Avoid relying on hover.
- Do not assume identical double-click timing or drag-event frequency.

### Window Management

- Do not assume title bars, borders, or decorations have identical dimensions.
- Do not position windows using hardcoded coordinates.
- Keep windows within the visible work area.
- Account for multiple monitors and mixed scaling.
- Do not assume the primary monitor begins at `(0, 0)`.
- Avoid fixed sizes unless required.
- Do not assume maximize, minimize, fullscreen, or always-on-top behavior is identical.
- Test modal dialogs and `grab_set()` behavior on each platform.

### High-DPI and Scaling

- Do not assume 96 DPI or 100% scaling.
- Avoid hardcoded pixel dimensions for text-dependent controls.
- Test at 100%, 150%, and 200% scaling where supported.
- Do not override Tk scaling without an explicit tested requirement.
- Ensure icons and images remain usable on high-resolution displays.
- Avoid raster assets that become unreadable when scaled.
- Do not infer physical size from pixel dimensions.

### Fonts and Text

- Prefer platform-default fonts or Tk named fonts.
- Do not assume Arial, Helvetica, Segoe UI, San Francisco, or any specific font exists everywhere.
- Provide tested fallbacks when a specific font is required.
- Do not assume equal character widths unless using a verified monospaced font.
- Handle Unicode input, composed characters, emoji, and non-ASCII text.
- Do not assume text measurement is identical across platforms.
- Avoid fixed widget widths based solely on character counts.

### Images and Icons

- Use formats supported consistently by the project's supported Tk versions.
- Prefer PNG for application images unless another format is required.
- Keep references to `PhotoImage` objects alive.
- Do not assume one application-icon format works for every platform.
- Isolate platform-specific icon handling.
- Do not use absolute development-machine paths.
- Verify transparent images across themes and platforms.
- Provide readable assets for light and dark environments where necessary.

### Themes and Appearance

- Do not assume a specific Ttk theme is available.
- Query available themes before selecting one.
- Provide a safe fallback to the platform default.
- Do not hardcode colors that fail in dark mode or high-contrast mode.
- Do not remove native focus indicators.
- Test custom styles on macOS, Windows, and common Linux desktops.
- Be conservative when overriding native appearance.
- Prefer semantic state changes over platform-specific style patches.

### Linux Variability

- Treat Linux as multiple desktop environments.
- Do not assume GNOME, KDE, Xfce, X11, or Wayland.
- Do not assume portals, notification services, system trays, keyrings, or browser launchers are available.
- Avoid depending on one distribution or package manager.
- Document required native packages separately from Python dependencies.
- Test both X11 and Wayland when relevant.

### Opening URLs, Files, and Folders

- Use `webbrowser` for URLs.
- Centralize opening files and folders behind a platform abstraction.
- Do not directly call `open`, `start`, or `xdg-open` from general application code.
- Validate paths and URLs before passing them to the OS.
- Handle missing default applications gracefully.
- Never construct shell commands from user-provided paths.

### Clipboard and Drag-and-Drop

- Use Tk clipboard APIs for normal text clipboard operations.
- Handle clipboard failures.
- Do not assume clipboard contents persist after application exit.
- Do not add drag-and-drop dependencies without verifying support on all platforms.
- Treat drag-and-drop as optional unless explicitly required.
- Provide a non-drag-and-drop path for essential workflows.

---

## Data and Persistence

- Keep persistence code outside widget classes.
- Validate persisted data before use.
- Use atomic writes when corruption is possible.
- Do not store secrets in source code or plain-text configuration.
- Handle missing, malformed, incompatible, or partially written files.
- Version persistent formats.
- Provide migration behavior when formats evolve.
- Do not persist absolute paths when a relocatable reference is sufficient.
- Handle configuration transferred between operating systems.
- Keep machine-specific values out of portable project configuration.
- Use cross-platform abstractions for keyring access and document fallback behavior.

---

## Date, Time, Locale, and Text Formats

- Do not assume a locale, date format, decimal separator, or clock format.
- Store machine-readable timestamps in explicit stable formats.
- Distinguish naive and timezone-aware datetimes.
- Do not parse user-facing dates with one platform-specific format.
- Avoid locale-sensitive sorting for identifiers.
- Do not assume newline conventions.
- Use text mode when newline translation is desired.
- Use explicit newline handling for external formats that require it.

---

## Packaging and Distribution

- Do not assume source execution and packaged execution share the same filesystem layout.
- Resource loading must work in development, tests, and packaged builds.
- Do not depend on `__file__` without accounting for the selected packaging system.
- Keep packaging-specific behavior separate from application logic.
- Declare runtime dependencies explicitly.
- Do not rely on undeclared system packages.
- Verify hidden imports, data files, Tcl/Tk resources, and dynamic modules.
- Maintain separate tested packaging configuration where required.
- Build distributable artifacts on the target operating system.
- Do not claim one artifact is portable across operating systems unless verified.
- Test packaged builds separately from source execution.

---

## Testing

- Keep business logic testable without starting Tk.
- Put parsing, validation, formatting, and transformation logic in pure functions where practical.
- Test controllers independently from widgets.
- Avoid arbitrary sleeps in tests.
- Use deterministic synchronization.
- Abstract external or slow dependencies.
- Test startup and normal shutdown.
- Test forced shutdown.
- Test cancellation.
- Test repeated opening and closing of dialogs.
- Test worker failure paths.
- Test behavior after widgets are destroyed.
- Test with a non-default working directory.
- Test read-only directories and unavailable files.
- Test paths with spaces and Unicode.
- Test keyboard-only navigation.
- Test high-DPI scaling and large system fonts.
- Test light and dark appearance where supported.
- Test packaged builds.
- Record OS, Python version, Tcl/Tk version, architecture, and packaging method in test reports.
- UI tests must not depend on exact pixel positions or fixed screen coordinates.

---

## Continuous Integration

- Run automated tests on macOS, Windows, and Linux.
- Do not merge platform-sensitive changes based on one operating system.
- Keep the supported Python-version matrix explicit.
- Verify imports and application startup on each platform.
- Run static analysis and unit tests consistently.
- Guard platform-specific tests with explicit markers.
- Do not silently skip tests because infrastructure is missing.
- Treat missing resources, unsupported Tcl/Tk versions, and packaging failures as release blockers when they affect supported systems.

---

## Logging and Diagnostics

- Use the `logging` module instead of scattered `print()` calls.
- Log startup and shutdown.
- Log unexpected exceptions with stack traces.
- Do not log passwords, tokens, secrets, or sensitive user data.
- Include enough context to diagnose failed operations.
- Keep user notifications separate from diagnostic logs.
- Avoid excessive logs from high-frequency UI events.
- Include platform, Python, and Tcl/Tk version information in diagnostic output where useful.

---

## Dependency Policy

- Prefer the Python standard library when sufficient.
- Do not introduce another GUI framework.
- Do not add dependencies without a clear requirement.
- Verify that every added dependency supports macOS, Windows, and Linux.
- Verify compatibility with all supported Python versions.
- Document native package requirements.
- Avoid dependencies that require a shell, desktop environment, or unsupported system service.
- Keep optional integrations optional.
- Provide graceful fallback behavior when optional dependencies are unavailable.

---

## Agent Implementation Rules

Agents must:

- Make small, reviewable changes.
- Modify only files relevant to the task.
- Preserve public interfaces unless the task requires a change.
- Update every reference when renaming classes, methods, callbacks, configuration keys, or resources.
- Preserve existing behavior unless explicitly asked to change it.
- Explain newly introduced concurrency mechanisms.
- Document ownership of threads, queues, futures, workers, and scheduled callbacks.
- Provide a clear shutdown path.
- Avoid placeholder exception handling.
- Avoid unfinished background-task logic.
- Verify callback signatures.
- Verify imports.
- Verify widget parents.
- Verify geometry-manager usage.
- Verify resource paths.
- Verify behavior when launched outside the repository directory.
- Verify all UI changes remain usable under scaling and platform-default fonts.
- Avoid OS-specific shell commands.
- Avoid assumptions about path separators, font availability, keyboard modifiers, screen size, or filesystem behavior.
- Add or update tests for every behavior change.
- Document unavoidable platform limitations.

Agents must not:

- Update Tk widgets from worker threads.
- Block the Tk event loop.
- Call `sleep()` in UI code.
- Create multiple `Tk()` roots.
- Mix geometry managers within the same parent.
- Use repeated `update()` calls to simulate asynchronous behavior.
- Introduce `shell=True` without an explicit, documented requirement.
- Hardcode development-machine paths.
- Write runtime data into installed application directories.
- Claim compatibility without evidence.
- Rewrite unrelated code.
- Introduce a new architecture for a small feature.
- Add dependencies solely for convenience when standard-library functionality is sufficient.

---

## Pre-Completion Checklist

Before completing a change, verify:

1. All Tk calls occur on the main thread.
2. No callback performs blocking or expensive work.
3. Worker results return through a thread-safe mechanism.
4. Background work cannot update destroyed widgets.
5. Shutdown cancels scheduled callbacks and handles active workers.
6. The application still uses exactly one `Tk()` root.
7. Additional windows use `Toplevel`.
8. Layouts do not depend on exact platform rendering.
9. No path, shell, font, shortcut, screen-size, or filesystem assumption is platform-specific.
10. Platform checks are centralized and justified.
11. The feature works from a non-default working directory.
12. Resources use the project resource-loading mechanism.
13. User data is written only to appropriate writable locations.
14. Keyboard and mouse behavior have portable fallbacks.
15. External commands are avoided or invoked without a shell.
16. macOS, Windows, and Linux behavior is defined.
17. Tests cover portable behavior and necessary platform-specific branches.
18. Errors leave the UI in a consistent state.
19. Logs contain useful diagnostics but no secrets.
20. Documentation identifies unavoidable limitations.
21. Cross-platform compatibility claims are supported by tests or CI.