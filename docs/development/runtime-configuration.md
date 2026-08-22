# Runtime configuration architecture

This document is authoritative for how CaveViewer runtime configuration is
owned and resolved. Commands, the generated environment-variable table,
storage locations, and troubleshooting remain in
[Source setup](source-setup.md#environment-variables). Cross-layer dependency
direction remains authoritative in [Architecture](architecture.md).

## Configuration categories

Every setting is a persisted preference with an optional environment override,
an environment-only runtime/developer setting, a command-line override, or
packaging/development-shell configuration that never enters application runtime
settings. `PreferenceSpec` owns persisted fields and validation. The runtime
registry owns environment-only parsers, defaults, bounds, diagnostic safety,
and documentation metadata.

## Resolution and transport

Application and benchmark composition resolve one immutable `RuntimeSettings`
snapshot after command-line overrides are known. They inject the snapshot or a
focused immutable subsection into consumers. Saving Preferences replaces the
session snapshot; it does not mutate `os.environ` as an implicit message bus.

Process-environment access is allowed only at classified edges: initial
composition, platform probes, child-process serialization, or documented
standalone compatibility entry points. The executable allowlist is
`tests/unit/test_runtime_environment_boundaries.py`; every exception requires
an ownership reason.

## Authority boundaries

| Concern | Authority |
| --- | --- |
| Persisted schema and validation | `core/preferences/schema.py` |
| Registry, precedence, parsing, and provenance | `core/preferences/runtime_settings.py` |
| Cross-layer injection | [Architecture](architecture.md#runtime-settings) |
| Commands, variables, and troubleshooting | [Source setup](source-setup.md#environment-variables) |
| Direct environment exceptions | `tests/unit/test_runtime_environment_boundaries.py` |

Startup diagnostics and the generated application-runtime table are derived
from the registry. Add settings there first; do not maintain parallel defaults
in application, viewer, diagnostics, and documentation code.
