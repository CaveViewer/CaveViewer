# Coding standards

## Python and dependencies

- Keep runtime code compatible with the Python version used by CI (currently
  Python 3.12) unless the supported version is deliberately changed everywhere.
- Prefer the standard library and existing dependencies. A new runtime
  dependency requires a concrete benefit and updates to development and all
  platform packaging inputs.
- Use absolute `caveviewer.*` imports. Avoid compatibility aliases unless an
  external API requires them.
- Start every .py file with a well-written doc string

## Design

- Keep policy separate from side effects. Pure parsing, validation, budgeting,
  and spatial-selection functions are easier to test and reuse.
- Model multi-stage UI and concurrent workflows as explicit state machines.
  Keep allowed transitions outside widgets and expose immutable snapshots when
  another thread or presentation layer needs to observe the state.
- Give modules one clear responsibility. Split a module when it combines
  independent policies or lifecycle owners, not merely because it crossed an
  arbitrary line count.
- Prefer explicit dataclasses or small typed values at component boundaries.
- Preserve dependency direction: `caveviewer.gui` may depend on
  `caveviewer.core`; core may not depend on GUI.

## Naming and typing

- Use descriptive snake-case names for functions and variables and PascalCase
  for classes.
- Add type annotations to new public functions and non-obvious callback or
  concurrency boundaries. Avoid annotations that simply restate a trivial
  local expression.
- Use domain aliases for repeated structural types, such as a three-dimensional
  chunk cell, when they clarify APIs across modules.

## Errors and logging

- Raise specific exceptions that preserve actionable context. User-facing
  workflows should translate them into clear messages at the UI boundary.
- Catch broad exceptions only at process, worker, cleanup, or best-effort
  boundaries where continuing is intentional. Log the failure or document why
  it is safe to suppress.
- Use `caveviewer.core.logging_utils` rather than `print` for runtime diagnostics. Never
  log secrets or dump the complete environment.

## Filesystem and data safety

- Any user action involving disk reads or writes may fail.
- Fail gracefully by capturing exceptions and adding errors in the log file
- Do not show exceptions to users in the UI.
- Do not run out of space when creating new data (e.g., map import).
- Write replaceable state to a temporary sibling and publish it atomically.
- On cancellation and failure, remove partial output while preserving the last
  valid cache or user file.
- Validate binary lengths, versions, and manifest types before trusting data.
- Do not silently change cache or update formats; version and document them.

## Memory management

- Always clean up unused resources.
- Do not assume unlimited memory: users may run the app on older hardware.
- Choose smooth rendering and faster frames/second rate over faster map imports.

## Concurrency and UI

- Document which thread owns mutable state and external resources.
- Bound producer queues that can retain decoded maps, images, or chunk payloads.
- Make shutdown and cancellation deterministic and idempotent where practical.
- Tk and OpenGL operations remain on their owning main thread. Workers return
  CPU-side data through explicit handoff points.

## Comments and documentation

- Explain invariants, constraints, and surprising tradeoffs. Do not narrate
  straightforward syntax.
- Keep docstrings accurate when behavior changes. Update architecture and
  configuration documentation in the same change that invalidates it.
- Separate mechanical file moves from edits so history remains traceable.
- Shell code under `scripts/` also follows `scripts/STANDARDS.md`.
