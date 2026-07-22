# GUI instructions

Applies to: `src/caveviewer/gui/`
Inherits: `/AGENTS.md`, `/src/AGENTS.md`
Overrides: none
Validation:

```bash
.venv-dev/bin/python -m pytest -p no:cacheprovider -q tests/unit/gui
```

These rules supplement repository and source instructions for Tk/OpenGL GUI
code. Detailed architecture is canonical in `/docs/development/architecture.md`;
runtime rendering behavior is explained in `/docs/development/rendering.md`.

## Ownership and boundaries

- `caveviewer.gui` may depend on `caveviewer.core`; core must not depend on
  GUI.
- GUI modules must not import upward into `caveviewer.app`; entry-point
  compatibility wrappers should call into GUI helpers rather than the other way
  around.
- Keep Tk presentation, OpenGL rendering, platform integration, and testable
  controller/model logic separated.
- Platform-specific behavior belongs behind `caveviewer.gui.platform` adapters
  rather than scattered `sys.platform`, `os.name`, or `platform.*` checks.
- Every GUI Python module should start with an ownership-focused module
  docstring. Avoid placeholder docstrings that only repeat the module path.
- Resource loading must work from source, tests, and packaged builds.

## Tk rules

- Create exactly one `Tk()` root. Use `Toplevel` for additional windows.
- All Tk widget and Tk variable operations must occur on the Tk main thread.
- Keep callbacks short. Do not run long work, blocking I/O, subprocess waits,
  thread joins, or `sleep()` calls on the UI thread.
- Pass worker results through a thread-safe channel and apply them through the
  Tk event loop.
- Cancel owned `after()` callbacks and guard callbacks that may run after a
  widget is destroyed.
- Keep business logic testable without constructing Tk widgets.

## OpenGL and rendering rules

- Treat OpenGL contexts and GPU objects as thread-affine resources.
- Create, upload, modify, and delete OpenGL resources only on the owning render
  thread unless a shared-context design is explicitly documented and tested.
- Workers may perform disk I/O, parsing, decompression, image decode, and
  CPU-side preparation, then hand prepared data to the render thread.
- Do not use Python locks as a substitute for OpenGL/GPU synchronization.
- Release pending GPU resources safely during shutdown and failed uploads.

## Responsiveness and portability

- Model loading, success, failure, cancellation, retry, and shutdown states
  explicitly.
- Provide deterministic cancellation and cleanup for background tasks.
- Do not depend on fixed screen size, DPI, fonts, titlebar geometry, path
  separators, shell behavior, or a specific Linux desktop session.
- Keep user-facing errors concise; log technical detail through the project
  logging facility without secrets.
- Preserve keyboard access, focus behavior, and non-mouse paths for essential
  actions.

## Tests

- Put GUI-adjacent logic that can run deterministically under
  `tests/unit/gui/`.
- `tests/unit/gui/test_gui_architecture_boundaries.py` enforces the GUI app
  import boundary, platform-check boundary, and module ownership docstrings.
- Test controller behavior independently from widgets where practical.
- Use bounded waits, events, fake adapters, and mocked worker results instead
  of timing-only sleeps.
- Cover worker failure, cancellation, shutdown, destroyed-widget callbacks, and
  platform branches touched by the change.
