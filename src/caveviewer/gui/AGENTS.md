# GUI instructions

These rules supplement the repository-level and source-level `AGENTS.md` files
for files under `src/caveviewer/gui/`.

- Tk widget mutations and OpenGL resource operations run only on their owning
  main thread. Background workers may perform disk I/O, parsing, image decode,
  and CPU payload preparation.
- Keep validation and state transitions outside widget construction where
  practical. Dialog modules should render controller/model state rather than
  duplicate business rules.
- Route operating-system behavior through `caveviewer.gui.platform` adapters. Keep the
  default adapter safe on unsupported platforms.
- Preserve focus, cancellation, cleanup, and retry semantics when changing
  dialogs. User cancellation should not leave partial downloads, caches,
  threads, timers, or GPU resources behind.
- Keep controls keyboard accessible and retain visible focus and validation
  feedback. Do not encode meaning through color alone.
- Update relevant tests in `tests/unit/gui/`. When visible UI changes make a
  documentation screenshot inaccurate, update or explicitly flag the image.
