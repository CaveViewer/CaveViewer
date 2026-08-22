# Code refactoring execution guide

This is the working plan for the next broad codebase refactors. Treat each
listed branch as an independent, behavior-preserving pull request from a fresh
`main`; do not stack unrelated refactors on an unmerged branch. Update this
guide when a discovered dependency changes the sequence or boundary below.

## Shared execution rules

- Start by characterizing existing behavior with focused tests. A refactor may
  move ownership and remove compatibility code, but it must not silently
  change precedence, UI behavior, platform routes, worker shutdown, or stored
  data formats.
- State the owner of every new mutable state machine, queue, timer, external
  resource, and native side effect in its module docstring and in
  `docs/development/architecture.md` when it creates a new cross-layer
  boundary.
- Use small typed values and dependency bundles at module boundaries. Do not
  replace a long constructor with an untyped `dict` or an object that reaches
  into globals.
- Keep Tk and OpenGL calls on their existing owning threads. Background work
  returns immutable data through an explicit queue/callback handoff; it never
  mutates a widget or GL resource directly.
- Retain a compatibility facade only while real production callers still need
  it. Give every facade a deletion condition, migrate callers, add a search or
  architecture test that proves the condition, then delete the facade in the
  same branch or the immediately following one.
- Before a pull request, run the focused tests named in its section, the
  affected package tests, the full suite, and `git diff --check`. Keep the PR
  description to a concise behavior/ownership summary; do not include
  validation output in it.

### Cross-platform filesystem-path and Windows-test standard

These rules apply to every refactor in this guide, especially when a path
crosses a configuration, worker, subprocess, cache, import, download, or
storage boundary.

- Treat a filesystem location as a structured path, not formatted text.
  Construct and join runtime paths with `pathlib.Path`; convert with
  `os.fspath()` only at an API boundary that requires a string or path-like
  value. Do not concatenate filesystem strings, hard-code path separators, or
  call `as_posix()` merely to make an assertion pass.
- Use forward-slash strings only for values whose format is intentionally
  platform-independent, such as URLs, archive member names, Git paths, and a
  documented wire format. A local cache, map, temporary, or user-data path
  must retain the native representation of the host that executes it.
- In tests, create local filesystem roots with `tmp_path` or a `Path` value
  and compare paths semantically. For example, prefer
  `Path(cache_dir).is_relative_to(cache_root)`, `.parent == cache_root`, or
  `.name == expected_name` over raw text checks such as
  `cache_dir.startswith("/cache-root")`. A POSIX-looking absolute string is
  not a portable assertion on Windows.
- When behavior deliberately parses or emits a foreign-platform path format,
  test that pure transformation with `PureWindowsPath` or `PurePosixPath`.
  Do not use a foreign pure-path object for host filesystem I/O, and keep that
  format conversion separate from normal local-path handling.
- Any changed path handoff to a worker, child process, cache/import service,
  or platform adapter needs a focused regression test that uses native path
  semantics and must pass the Windows essential test job before the PR is
  considered ready. Fix an OS-only assertion or normalization failure in the
  same branch; do not treat a Linux-only pass as sufficient evidence.

## Current recommended order

This order was re-audited against `main` at `3ce7f5c` on 2026-08-22. Completed
workstreams remain in the master table because their boundaries still matter,
but they are not branches to recreate.

| Order | Refactor | Primary branch | Why it comes here |
| --- | --- | --- | --- |
| 1 | Audit and retire runtime-settings fallbacks | `refactor/runtime-settings-fallback-retirement` | Confirms which remaining direct environment reads are intentional boundaries and prevents the compatibility path from spreading. |
| 2 | Bundle Map Library dependencies | `refactor/map-library-workflow-dependencies` | Shrinks the roughly 40-argument composition surface before a controller inherits it. |
| 3 | Extract splash lifecycle control | `refactor/splash-controller-lifecycle` | Builds on `SplashSession` and the dependency bundles without duplicating scheduler ownership. |
| 4 | Extract Map Library catalog/download and cache-rebuild orchestration | Existing workstream branches 10 and 11 | Separates worker lifecycles before deleting the coordinating facade. |
| 5 | Clean up splash composition | `refactor/splash-composition-cleanup` | Deletes forwarding and nested-function compatibility code only after direct composition exists. |
| 6 | Reduce the viewer-window composition surface | `refactor/viewer-window-composition-boundary` | Applies the same ownership model to the remaining 7,924-line GUI composition/controller module. |
| 7 | Consolidate documentation authority | `refactor/architecture-document-authority` | Records stable implementation boundaries and then splits the oversized operational reference. |

The branch names below are deliberately more granular than the table. Use the
smallest branch that produces a coherent, reviewable change; each name begins
with `refactor/` by convention.

## Master implementation status

Status and applicability were reviewed against `main` at `3ce7f5c` on
2026-08-22 (America/New_York). **Applies** means a new implementation branch is
still justified. **Enforce only** means the desired architecture is present and
should be protected, not reimplemented. **Fold** means any small remainder
belongs in another listed branch. A dash in **Commit / reference** means no
implementation commit exists yet.

| # | Workstream | Branch | Implementation status | Applicability on current `main` | Commit / reference | GitHub issue | Current handoff / boundary |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | Runtime settings source of truth | `refactor/runtime-settings-snapshot` | Implemented — merged | Enforce only | `64dcd91` (PR #221) | — | The immutable snapshot remains the correct composition boundary. Do not recreate this branch. |
| 2 | Runtime settings consumer migration | `refactor/runtime-settings-consumers` | Implemented — merged | Partially applies through new workstream 16 | `f9716f8`, `1ddd679`; merged as `0f7b6c2` (PR #222) | — | Production app and benchmark composition use typed settings. Remaining optional environment fallbacks require classification and eventual retirement, not a repeat migration. |
| 3 | Runtime settings diagnostics | `refactor/runtime-settings-diagnostics` | Implemented — folded into PR #222 | Enforce only | `f9716f8`; merged as `0f7b6c2` (PR #222) | — | Registry-derived safe diagnostics and the validated source-setup table remain current. Do not create a separate branch. |
| 4 | Platform presentation profile | `refactor/platform-presentation-profile-actions` | Implemented — merged | Enforce only | `e224b55`; merged as `0973c4a` (PR #234) | — | Static presentation policy still belongs exclusively to `PresentationProfile`. |
| 5 | Platform presentation actions | `refactor/platform-presentation-profile-actions` | Implemented — merged | Enforce only | `e224b55`; merged as `0973c4a` (PR #234) | — | Direct focused action adapters remain the correct boundary for DPI, About registration, and focus. |
| 6 | Native focused action adapters | `refactor/platform-native-action-adapters` | Implemented — merged | Enforce only | `086ac59`; merged as `2508596` ([PR #251](https://github.com/CaveViewer/CaveViewer/pull/251)) | [#224](https://github.com/CaveViewer/CaveViewer/issues/224) — closed | Focused adapters own artifact reveal, recording startup, and TLS trust. |
| 7 | Remove broad splash platform adapter | `refactor/remove-splash-platform-adapter` | Implemented — merged | Enforce only | `8e70397`; merged as `0e21f7d` ([PR #255](https://github.com/CaveViewer/CaveViewer/pull/255)) | [#225](https://github.com/CaveViewer/CaveViewer/issues/225) — closed | Production references are absent and an architecture guard prevents the retired API from returning. The previous “PR open” status was stale. |
| 8 | Splash controller lifecycle | `refactor/splash-controller-lifecycle` | Implemented — merged | Enforce only | `197e68d` ([PR #273](https://github.com/CaveViewer/CaveViewer/pull/273)) | [#226](https://github.com/CaveViewer/CaveViewer/issues/226) — closed | A widget-free controller owns start, selection, scheduling, and idempotent close while reusing `SplashSession` as the callback-token owner. |
| 9 | Map Library workflow dependencies | `refactor/map-library-workflow-dependencies` | Implemented — merged | Enforce only | `9fcbb97` ([PR #272](https://github.com/CaveViewer/CaveViewer/pull/272)) | [#227](https://github.com/CaveViewer/CaveViewer/issues/227) — closed | The loose constructor surface is replaced by six typed composition, storage, catalog, download, action, and cache-rebuild dependency bundles. |
| 10 | Map Library catalog and download workflows | `refactor/map-library-catalog-download` | Implemented — merged | Enforce only | `59f5ac1` ([PR #274](https://github.com/CaveViewer/CaveViewer/pull/274)) | [#228](https://github.com/CaveViewer/CaveViewer/issues/228) — closed | Focused catalog and download lifecycle owners manage worker startup, queues, polling, cancellation, inhibition, terminal delivery, and close cleanup; Tk rendering remains in the coordinator. |
| 11 | Map Library cache-rebuild orchestration | `refactor/map-library-cache-rebuild` | Implemented — merged | Enforce only | `3e61437` ([PR #275](https://github.com/CaveViewer/CaveViewer/pull/275)) | [#229](https://github.com/CaveViewer/CaveViewer/issues/229) — closed | A focused orchestration owner wraps `CacheRebuildJobController` and owns the Tk polling token, pause/close requests, and typed update delivery without duplicating process state. |
| 12 | Splash composition cleanup | `refactor/splash-composition-cleanup` | Implemented — merged | Enforce only | `beae687` ([PR #276](https://github.com/CaveViewer/CaveViewer/pull/276)) | [#230](https://github.com/CaveViewer/CaveViewer/issues/230) — closed | Obsolete catalog, download, and rebuild polling/cancellation facades are removed; composition and tests call the focused lifecycle owners directly, with a regression guard preventing facade return. |
| 13 | Architecture document authority | `refactor/architecture-document-authority` | Not implemented — tracked | Applies, narrowed | — | [#231](https://github.com/CaveViewer/CaveViewer/issues/231) | `architecture.md` (804 lines) and `source-setup.md` (876 lines) still overlap. Establish the authority/link map after implementation boundaries stabilize. |
| 14 | Platform documentation authority | `refactor/platform-documentation-authority` | Desired outcome already present | Fold into 13 | — | [#232](https://github.com/CaveViewer/CaveViewer/issues/232) — closed | The 84-line focused platform guide already describes current adapters and no longer presents the broad adapter as the target. Only link/de-duplication work remains under #231. |
| 15 | Scoped agent-guide cleanup | `refactor/scoped-agent-guide-cleanup` | Desired outcome already present | No standalone branch | — | [#233](https://github.com/CaveViewer/CaveViewer/issues/233) — closed | Current scoped guides are short, actionable, and link canonical development docs. Revisit only if future guides accumulate narrative duplication. |
| 16 | Runtime-settings fallback retirement | `refactor/runtime-settings-fallback-retirement` | Implemented — merged | Enforce only | `567b2a2` ([PR #271](https://github.com/CaveViewer/CaveViewer/pull/271)) | [#268](https://github.com/CaveViewer/CaveViewer/issues/268) — closed | Remaining direct environment access is classified by ownership and an architecture test requires every module-level exception to stay explicitly allowlisted. |
| 17 | Viewer-window composition boundary | `refactor/viewer-window-composition-boundary` | Implemented locally — review pending | Continue incrementally after merge | `bba5204` | [#269](https://github.com/CaveViewer/CaveViewer/issues/269) | Pure benchmark metadata composition and fingerprinting are extracted from `viewer_window.py`; later viewer slices should use separate issues rather than extending this branch. |
| 18 | Source-setup operational reference split | `refactor/source-setup-reference-split` | Suggested — tracked | Fold after 13 | — | [#270](https://github.com/CaveViewer/CaveViewer/issues/270) | After the authority map, split commands/environment reference from long subsystem explanations while preserving generated settings markers and stable links. |

## 1. Establish one runtime-settings source of truth

### Outcome and boundary

`PreferenceSpec` remains the authority for persisted preference fields,
validation, ranges, and preference-to-environment conversion. Add a separate
typed runtime-settings registry for environment-only settings and compose one
immutable `RuntimeSettings` snapshot after command-line overrides have been
applied. Consumers receive the snapshot or a narrowly typed subsection instead
of reading `os.environ` independently.

Do not force every `CAVEVIEWER_*` variable into the Preferences dialog. A
setting can be one of these categories:

- persisted preference with an optional environment override;
- environment-only runtime/developer setting;
- command-line-only launch override; or
- packaging/development-shell setting that must not enter application runtime
  settings.

The registry must state the category, parser, built-in default, accepted range
or enum values, display description, documentation visibility, and whether the
value is safe to include in startup diagnostics. This replaces the parallel
catalogues currently spread through `PreferenceSpec`, `app.py`,
`viewer_window.py`, and individual core services.

### Suggested branches

1. `refactor/runtime-settings-snapshot`
2. `refactor/runtime-settings-consumers`
3. `refactor/runtime-settings-diagnostics`

Do not start branch 2 until branch 1 has merged. Branch 3 may follow branch 2
or be folded into it only if the consumer migration stays small.

### Detailed implementation steps

1. Inventory every current read of `CAVEVIEWER_*`, every CLI override, and
   every persisted preference. Record the current owner, parser, default,
   precedence, and consumers in a characterization test or temporary planning
   table before moving code. Include settings currently absent from
   `app.py`'s startup catalog, especially recording and Map Library storage
   settings.
2. Add a core-only module such as
   `src/caveviewer/core/preferences/runtime_settings.py`. It must not import
   GUI, Tk, OpenGL, or `caveviewer.app`.
3. Keep `PreferenceSpec` focused on persisted fields. Define a complementary
   `RuntimeSettingSpec` for non-persisted variables, and let a runtime entry
   reference a `PreferenceSpec` rather than copying its validation metadata.
   Use immutable dataclasses for the resolved snapshot and for source
   provenance, for example `SettingSource.BUILT_IN`, `PREFERENCES`,
   `ENVIRONMENT`, and `CLI`.
4. Make source precedence explicit and test it for every category. First
   characterize current behavior; preserve it unless a product decision changes
   it. Do not assume environment variables override saved preferences merely
   because both exist. CLI overrides must be applied before the single
   composition call, not patched into a finished snapshot later.
5. Make composition accept injected mappings and platform facts rather than
   reading global process state throughout the code. A shape such as
   `resolve_runtime_settings(preferences, environ, cli_overrides, platform)`
   keeps tests deterministic and permits workers to receive a serializable
   subset.
6. Replace direct environment reads in `app.py`, `viewer_window.py`, import
   launch code, recording configuration, Map Library storage selection, and
   relevant core services with the snapshot or a narrow typed subsection. Pass
   settings through existing composition roots and worker launch requests. Do
   not make a worker depend on a parent process mutating `os.environ` as an
   implicit transport.
7. Keep a temporary compatibility bridge only where a spawned legacy entry
   point still requires environment variables. Isolate it at process launch,
   document it as one-way serialization of the resolved snapshot, and delete
   it after that entry point accepts typed settings.
8. Replace `_KNOWN_CAVEVIEWER_ENV_VARS` and effective-default tables in
   `app.py` with startup diagnostics derived from the registry. Preserve the
   existing rule that unrelated environment variables are never dumped, and
   redact any future setting marked diagnostic-unsafe.
9. Generate or validate the environment-variable table in
   `docs/development/source-setup.md` from the same registry. Use stable
   begin/end markers around the generated table or a deterministic renderer in
   a documentation test so a new setting cannot update code while leaving docs
   and diagnostics stale.
10. Delete duplicate parsers/default constants only after all consumers use
    the snapshot. Keep per-feature policy constants when they are genuine
    behavior rather than configuration.

### Completion criteria and verification

- One tested resolver owns runtime precedence, parsing, defaults, range
  validation, and source provenance.
- Adding a runtime setting requires one registry entry and tests; it does not
  require editing parallel lists in `app.py` and the viewer.
- Preferences persistence still stores only declared persisted fields and bad
  saved/environment values fall back independently.
- Startup diagnostics list the complete safe catalog and report effective
  values from the resolved snapshot.
- Run focused preference/schema, application composition, viewer, import, and
  recording tests; then `tests/unit/core`, `tests/unit/gui`, the full suite,
  and `git diff --check`.

### Execution handoff — 2026-08-19

The master implementation-status table above is authoritative for branch
completion; this section preserves the runtime-settings-specific rationale.

- `refactor/runtime-settings-snapshot` merged as PR #221 at `64dcd91`.
- `refactor/runtime-settings-consumers` merged as PR #222 at `0f7b6c2`. Its
  implementation commits are `f9716f8` (consumer migration) and `1ddd679`
  (portable import-path regression test). The interactive application and
  benchmark entry points now compose one `RuntimeSettings` snapshot; platform
  policy, splash/Map Library, viewer, and import-child requests receive that
  snapshot or a focused immutable subsection. Preferences saves replace the
  session snapshot instead of mutating `os.environ`.
- The diagnostics/documentation scope proposed for
  `refactor/runtime-settings-diagnostics` was folded into this branch: startup
  diagnostics use the registry's safe effective values, and the marked runtime
  table in `source-setup.md` is validated against a deterministic registry
  renderer. Do not create a duplicate diagnostics branch.
- Optional legacy parameters on low-level core/GUI helpers still fall back to
  process environment only for standalone callers that have not supplied a
  snapshot. No production app or benchmark composition path uses that bridge;
  delete the fallback once those standalone compatibility callers are retired.
- PR #222 completed successfully: Essential Tests (including Windows unit
  tests), Linux/macOS unit tests, and Linux/macOS package-smoke checks passed.
  The Windows-inclusive Essential Tests run is
  [32326364728](https://github.com/CaveViewer/CaveViewer/actions/runs/32326364728).
- The former next step, platform-adapter migration, is now complete. The
  remaining settings action is workstream 16: inventory direct environment
  reads and retire only compatibility fallbacks whose standalone callers no
  longer exist.

### Current applicability review — 2026-08-22

- Workstreams 1 and 3 remain valid completed architecture and need enforcement,
  not new implementation branches.
- Workstream 2's primary consumer migration is complete. Direct environment
  reads still exist in viewer, streaming, chunking, texture, storage, logging,
  DPI, and platform-probe code. Some are legitimate process/platform facts or
  deliberately supported standalone defaults, so blanket replacement would be
  incorrect.
- Workstream 16 must classify each remaining read. Add a boundary test with an
  explicit allowlist for intentional reads, migrate proven legacy fallbacks,
  and document a deletion condition for every retained compatibility read. Do
  not make the architecture test depend on a raw count.

## 2. Finish shrinking the broad platform adapter

### Outcome and boundary

`SplashPlatformAdapter` is a compatibility surface, not the permanent platform
API. Its remaining methods currently mix static presentation choices, native
presentation actions, artifact reveal, TLS trust augmentation, recording
startup, input labels, and backend sizing. Move each remaining concern to a
focused immutable profile, probe/policy pair, or action adapter. When no
production callers remain, remove the broad adapter protocol, factory paths,
and compatibility implementation classes together.

Preserve the current macOS, Windows, Linux, and fallback behavior exactly. In
particular, do not turn native work into import-time side effects, weaken update
TLS verification, replace portal fallbacks, or change the meaning of a user
shortcut while moving a method.

### Suggested branches

1. `refactor/platform-presentation-profile`
2. `refactor/platform-presentation-actions`
3. `refactor/platform-native-action-adapters`
4. `refactor/remove-splash-platform-adapter`

Branches 1–3 may be reviewed independently. Branch 4 is deletion-only except
for necessary composition and test updates, and should start only after a
repository-wide caller inventory is empty.

### Detailed implementation steps

1. Inventory every `SplashPlatformAdapter` method, its implementations, and
   all production callers. Group each method by ownership: static presentation
   convention, action-time native effect, feature probe/policy, focused file or
   process action, or obsolete compatibility behavior.
2. Freeze the broad protocol: no new feature may add a method to it. Add an
   architecture-boundary test or review check that rejects new production
   references while the migration is underway.
3. For static values such as font candidates, layouts, shortcut labels, mouse
   conventions, scaling, and backend sizing, extend `PresentationProfile` only
   where it lacks a value. Select it from process-stable facts without creating
   Tk objects, touching a display, or invoking native APIs. Migrate each caller
   to `PlatformRuntime.presentation_profile` or an explicit pure fallback.
4. For action-time native work such as DPI setup, About-menu registration, and
   viewer focus, make `PresentationActionsAdapter` own direct platform
   implementations. It may temporarily delegate to the broad adapter only
   until the equivalent native implementation is moved; record that delegation
   and removal condition in the module docstring.
5. For focused file reveal, recording `Popen` kwargs, and TLS trust
   augmentation, migrate the existing narrow facades to direct focused
   implementations. Each should receive only the capability/target it needs,
   retain current best-effort failure behavior, and never import a GUI caller
   upward into `app.py`.
6. Keep policy separate from execution. A static or action-time probe reports
   facts, a pure policy returns a typed decision, and an injected adapter
   performs the action. Do not replace the broad adapter with another broad
   service locator.
7. Update `PlatformRuntime` composition one focused adapter at a time. Inject
   the new adapter into splash, viewer, preferences, update, and Map Library
   callers before removing the corresponding broad-adapter method.
8. After each migration, search for the old method and remove obsolete
   compatibility wrappers, imports, mocks, and default implementations. Keep
   a compatibility facade only if a real caller remains; tests alone are not a
   reason to preserve it.
9. Before the deletion branch, prove with `rg` and an AST architecture test
   that no production module imports `SplashPlatformAdapter`, calls
   `get_platform_adapter()`, or reads `PlatformRuntime.platform_adapter` for a
   migrated concern. Then remove `base.py`, the broad factory aliases, and
   former platform subclasses only when their methods have no focused owner
   left.
10. Rewrite `src/caveviewer/gui/platform/platform-adapters.md` after the code
    is stable so it describes focused contracts rather than presenting the
    retired broad protocol as the architecture.

### Completion criteria and verification

- Platform composition contains focused, typed adapters/profiles only; it has
  no catch-all compatibility object.
- Static profile selection is side-effect free, while native work remains
  action-time and injected.
- Windows console suppression, macOS About/focus behavior, Linux portal and
  fallback routes, TLS handling, update-package reveal, and saved-artifact
  reveal retain their existing contracts.
- Run platform profile/runtime/adapter tests, update and recording tests,
  `tests/unit/gui/test_gui_architecture_boundaries.py`, platform packaging
  tests, the full suite, and `git diff --check`.

### Execution handoff — 2026-08-20

- User-directed exception: items 4 and 5 were completed together on
  `refactor/platform-presentation-profile-actions`, freshly based on `main` at
  `5bb994d`, as commit `e224b55`. The existing older dirty branch and historical
  stash were left untouched.
- Static font, layout, shortcut/input, scaling, startup-focus, and backend
  sizing policy now lives exclusively in `presentation.py`. The three layout
  value types moved there with `PresentationProfile`; no static presentation
  method remains on the broad protocol or its compatibility implementations.
- `PresentationActionsAdapter` now selects direct Windows, macOS, or fallback
  implementations from the composed platform fact. It owns only DPI setup,
  macOS About-menu registration, and viewer focus; its factory and methods do
  not depend on `SplashPlatformAdapter`.
- The architecture-boundary tests reject both static presentation methods and
  native presentation actions on `SplashPlatformAdapter`, while focused
  regression tests preserve Windows DPI fallbacks and macOS About/focus
  behavior.
- Verification passed: focused profile/splash/macOS/runtime/architecture tests,
  the GUI suite (`1104 passed`), the full suite (`1653 passed`), and
  `git diff --check`.
- Next: workstream 6 is tracked by [#224](https://github.com/CaveViewer/CaveViewer/issues/224);
  the remaining handoffs are linked in the master table.

### Execution handoff — 2026-08-22

- Workstream 6 was implemented on `refactor/platform-native-action-adapters`,
  freshly based on clean `main` at `379ba5c`, as commit `086ac59`, and merged
  as `2508596` in [PR #251](https://github.com/CaveViewer/CaveViewer/pull/251).
- `SavedArtifactRevealAdapter` now selects direct Finder, Explorer, Linux
  desktop-service, or safe unsupported behavior from stable platform facts.
- `RecordingProcessAdapter` directly owns Windows `STARTUPINFO` and
  `CREATE_NO_WINDOW`; other platforms retain empty encoder launch options.
- `TlsTrustAdapter` directly augments Windows contexts from the `CA` and
  `ROOT` stores without changing verification; other platforms retain the
  normal default context roots.
- `PlatformRuntime` composes all three focused adapters without passing
  `SplashPlatformAdapter`. The migrated methods were removed from the broad
  protocol and implementations, and an AST boundary test prevents regression.
- Verification passed: 323 focused platform/viewer/update/architecture tests,
  the full suite (`1729 passed`), and `git diff --check`.
- Workstream 7 was implemented on `refactor/remove-splash-platform-adapter`,
  freshly based on clean `main` at `c187b11`, as commit `8e70397`. Review and
  merge completed as `0e21f7d` in
  [PR #255](https://github.com/CaveViewer/CaveViewer/pull/255).
- The broad protocol, factory aliases, runtime property, platform compatibility
  classes, and legacy consumer fallbacks are deleted. Focused adapters are
  composed directly, and an architecture guard requires the retired modules to
  stay absent and rejects production references to their former API.
- Verification passed: 263 focused platform/viewer/update/architecture tests,
  the full suite (`1725 passed`), and `git diff --check`. Ruff was not installed
  in the development environment, so no Ruff artifact is available.
- The retired broad-adapter names remain only in the architecture regression
  test that proves they stay removed. No production caller or factory remains.
- No additional platform implementation branch applies. Keep the focused
  adapters and removal guard intact; fold documentation link/de-duplication
  cleanup into workstream 13.

## 3. Split the splash composition workflow

### Outcome and boundary

`show_splash_screen()` becomes a small Tk composition boundary: it creates the
single root, constructs widgets, wires explicit callbacks, starts the
controller, and enters/leaves the event loop. A testable `SplashController`
owns splash lifecycle transitions, scheduled-callback ownership, update/map
actions, and orderly teardown without creating or mutating widgets directly.

`MapLibraryWorkflow` becomes a thin coordinator over focused catalog/download,
cache-rebuild, and map-action workflows. Its current large injection list is
replaced by a few typed dependency bundles and explicit ports. Workers continue
to return data through queues; only Tk-thread callbacks apply it to panels.

### Suggested branches

1. `refactor/map-library-workflow-dependencies`
2. `refactor/splash-controller-lifecycle`
3. `refactor/map-library-catalog-download`
4. `refactor/map-library-cache-rebuild`
5. `refactor/splash-composition-cleanup`

Keep branches 3 and 4 separate: catalog/download activity and cache rebuilding
have different worker lifecycles, cancellation rules, and user-visible states.
Branch 5 removes temporary facade methods only after the focused workflows are
in use.

### Detailed implementation steps

1. Map the current nested functions in `show_splash_screen()` by owner before
   moving them: root/window lifecycle, update presentation, folder selection,
   Preferences, Map Library, scheduled polling, and close/teardown. Add
   characterization tests for close, destroy, failed worker, cancellation,
   retry, and an `after()` callback arriving after teardown.
2. Define small protocol/value boundaries before moving code. Typical examples
   are a `SplashView` for render requests, a `TkScheduler` abstraction for
   `after`/`after_cancel`, and immutable view-state snapshots. The controller
   may call injected callbacks, but it must not import `tkinter` or hold raw
   widget references.
3. Introduce `SplashController` alongside the existing function. Reuse the
   existing `SplashSession`, which already owns scheduled callback identifiers,
   cancellation, result state, close state, and idempotent shutdown. The new
   controller coordinates that owner; it must not duplicate its callback
   registry or create competing close state. `show_splash_screen()` remains
   the only place that constructs `Tk()` and adapts controller output to
   widgets.
4. Migrate one nested-function family at a time into controller methods. Keep
   the old public behavior and callback wiring intact between moves; do not
   combine a layout redesign, update UX change, or new map feature with this
   lifecycle refactor.
5. Replace the `MapLibraryWorkflow` constructor's individual callback list
   with purpose-specific immutable dependency bundles, for example catalog
   service, managed-map storage, desktop actions, launch actions, scheduler,
   and feedback port. Each bundle should expose only the methods its workflow
   needs, preserving test injection without a 41-parameter constructor.
6. Extract a catalog workflow that owns catalog-worker startup, result queue
   draining, catalog reconciliation, former-map handling, and refresh state.
   It produces row/view-model updates; `MapLibraryPanel` remains the Tk owner
   that renders them.
7. Extract a download workflow that owns one active download's cancel event,
   worker/result queue, scoped desktop inhibition, completion/failure state,
   and cleanup. It must release inhibition exactly once and ignore late worker
   results after cancellation or splash teardown.
8. Extract a cache-rebuild workflow that owns preflight, the rebuild job
   controller, polling `after()` identifier, completion notification policy,
   and close-time cancellation/cleanup. Keep rebuild process work outside the
   Tk thread and preserve the current cooperative pause/checkpoint behavior.
9. Keep Guided Dive, open-map, delete-map, and cave metadata actions behind a
   small map-action coordinator or explicit callbacks. Do not embed them back
   into catalog/download/rebuild classes merely because they originate from a
   row click.
10. During migration, retain `MapLibraryWorkflow` only as a forwarding facade
    with a clear deletion condition. Once `SplashController` composes the new
    workflows directly, remove the facade, obsolete nested functions, and
    duplicate polling ownership.
11. Make teardown order explicit and test it: stop accepting UI input, cancel
    owned `after()` callbacks, request cooperative worker cancellation, release
    scoped inhibitors, discard late queue results, then destroy the root only
    if this splash owns it. No callback may call a destroyed widget.

### Completion criteria and verification

- `show_splash_screen()` is composition and view wiring rather than a second
  controller with dozens of nested functions.
- `SplashController` and each Map Library workflow can be unit tested with
  fake scheduler, panel/view port, queues, workers, desktop service, and
  clock—without creating Tk widgets.
- Exactly one `Tk()` root remains; all Tk operations occur on its main thread;
  every owned `after()` call has an owner and a cancellation path.
- Catalog refresh, download, cache rebuild, Guided Dive, recent-map actions,
  desktop inhibition, foreground feedback, and notification behavior remain
  unchanged from the user's perspective.
- Run splash, map-library workflow/controller/panel, cache-rebuild, Guided
  Dive, and GUI architecture tests; then `tests/unit/gui`, the full suite, and
  `git diff --check`.

### Current applicability review — 2026-08-22

- All five workstreams still apply, but dependency bundling now precedes the
  splash controller. `MapLibraryWorkflow` is 1,967 lines and its constructor
  receives roughly 40 individual services, callbacks, factories, queues, and
  policy values. Moving that surface unchanged into a controller would only
  relocate the problem.
- `SplashSession` is a useful completed foundation, not evidence that
  workstream 8 is finished. `splash_screen.py` is still 2,377 lines and the
  composition function retains nested UI/workflow callbacks and direct
  scheduling responsibilities.
- Catalog and download may share a branch for review economy, but they must be
  separate lifecycle owners. Cache rebuilding already has a focused process
  controller; workstream 11 extracts orchestration around it rather than
  replacing it.
- Add architecture tests for ownership boundaries after extraction—for
  example, preventing the coordinator from directly scheduling Tk polling or
  accepting new loose callbacks. Avoid brittle line-count or method-count
  limits.

## 4. Consolidate duplicated architecture documentation

### Outcome and boundary

Keep `docs/development/architecture.md` concise and authoritative for
cross-layer ownership, dependency direction, process/runtime composition, and
state-machine summaries. Keep subsystem mechanics in the focused document that
owns them. `source-setup.md` remains a source/development guide and references
the authoritative update/platform contracts instead of repeating them.

`AGENTS.md` files are short, enforceable local guardrails and validation
commands—not architecture history or a duplicate of development documentation.
The current tree no longer contains a navigation-specific `AGENTS.md` after
navigation-certificate removal; apply the cleanup rule to any future scoped
instruction file or legacy branch that still contains one.

### Suggested branches

1. `refactor/architecture-document-authority`
2. `refactor/source-setup-reference-split` if the first branch would otherwise
   become too large

Do not create platform-documentation or scoped-agent cleanup branches solely
to satisfy the old table. Their intended outcomes are already present on
current `main`; include only concrete link/de-duplication edits found while
establishing the authority map.

### Detailed implementation steps

1. Inventory duplicated sections by concept, not merely repeated phrases:
   platform runtime contract, capability/policy/action flow, update state,
   package storage/reveal, recording, viewer boundary, environment settings,
   and test/agent rules. Record the current location and intended canonical
   location in a small mapping table before deleting prose.
2. Define a documentation authority map. At minimum:

   | Concept | Canonical location | Other documents should do |
   | --- | --- | --- |
   | Cross-layer architecture and ownership | `architecture.md` | Link to the relevant heading. |
   | Platform adapter routes and implementation mechanics | `src/caveviewer/gui/platform/platform-adapters.md` | Link back to architecture for the general contract. |
   | Update behavior and development override use | `architecture.md` plus `source-setup.md` for commands/variables | Keep one state-machine definition; elsewhere summarize and link. |
   | Environment-variable reference | `source-setup.md`, generated/validated from the runtime-settings registry | Architecture explains ownership, not every variable. |
   | Enforceable local instructions | nearest `AGENTS.md` | Link to docs instead of copying narrative. |

3. Edit `architecture.md` first. Keep diagrams and prose at the boundary level:
   owners, allowed dependency direction, thread/resource ownership, typed
   composition, and concise state transitions. Replace long subsystem mechanics
   with an anchored link to their focused document.
4. Edit `platform-adapters.md` to contain platform-specific routes, focused
   adapter protocols, platform behavior differences, and migration notes that
   are still true. Remove descriptions of retired broad adapters as the target
   architecture.
5. Edit `source-setup.md` to retain commands, supported environment variables,
   troubleshooting, and release/development procedures. Replace duplicated
   update-state prose with a short explanation plus a stable link to the
   authoritative architecture section.
6. Shorten each scoped `AGENTS.md` to inheritance, ownership constraints,
   thread/resource rules that are locally enforceable, and focused validation
   commands. Move explanatory history, design rationale, and long lists of
   platform details into development documentation.
7. Verify all relative links and heading anchors after moves. Prefer one
   clearly named heading over repeated near-identical headings, and avoid
   redirect-like documents that merely restate the first paragraph of another
   document.
8. Add lightweight documentation checks where practical: required canonical
   headings/links, generated settings-table agreement, and scoped `AGENTS.md`
   shape. Do not use brittle tests that require prose wording to stay exact.

### Completion criteria and verification

- A contributor can identify the authoritative document for each architecture
  concept from the mapping table and links alone.
- The update state machine and platform contract have one detailed source of
  truth; other pages link rather than copy it.
- Scoped agent instructions are quick to scan and contain only actionable
  policy/validation relevant to their directory.
- Run documentation/link checks, the affected architecture-boundary tests, the
  full suite if executable checks changed, and `git diff --check`.

### Current applicability review — 2026-08-22

- Workstream 13 still applies, but should be an authority-and-links pass rather
  than a broad prose rewrite. The two primary development documents are still
  large enough—804 and 876 lines—that ownership and operational instructions
  are difficult to locate.
- Workstream 14 no longer warrants a standalone branch. The focused platform
  guide is concise, names the current profiles/adapters, and does not describe
  the retired broad adapter as the target architecture.
- Workstream 15 no longer warrants a standalone branch. The root and scoped
  `AGENTS.md` files are short actionable inheritance, ownership, and validation
  guides. Preserve that shape and revisit only when concrete duplication
  returns.
- Workstream 18 is an optional second slice after the authority map: keep
  source setup focused on commands, environment reference, troubleshooting,
  and release procedures, and link to architecture for subsystem mechanics.

## 5. Additional current-code suggestions

### Viewer-window composition boundary

`src/caveviewer/gui/viewer_window.py` is 7,924 lines. It is both a composition
root and an owner/coordinator for map loading, asynchronous texture validation,
benchmark route/configuration, capture and recording state, slice operations,
and input/render behavior. Existing focused controllers show the intended
direction, but the main class still carries extensive forwarding state and
workflow implementation.

Start only after splash ownership stabilizes, and extract one characterized
workflow per pull request. First inventory fields, scheduled/background work,
and delegated controller properties; then select the smallest owner with an
explicit lifecycle. Preserve the OpenGL-thread boundary and do not convert the
module into a generic service locator. Add dependency-direction tests for each
extracted owner instead of enforcing a target file size.

### Durable boundary guards

Add or extend AST/import boundary tests when completing workstreams 8–12, 16,
and 17. Guards should prohibit a specific architectural regression—new direct
environment reads outside an allowlist, loose workflow callbacks, Tk scheduling
in a worker owner, or imports across a declared layer. They should not freeze
implementation details such as exact prose, method counts, constructor counts,
or line totals.

## Future refactor handoff checklist

Before starting any branch in this guide, record these items in the task or PR
notes:

1. Branch name and the exact `main` commit it starts from.
2. The owner being introduced or retired, its thread/process ownership, and
   the compatibility code that will be deleted.
3. The behavior characterization tests that protect current semantics.
4. The smallest complete implementation slice and explicit non-goals.
5. Searches/tests that prove callers have migrated and the deletion condition
   is true.

At handoff, update this document with completed branch references, newly found
dependencies, or intentionally deferred work so the next refactor begins from
observed repository state rather than assumptions.
