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

### Log information

Help > Troubleshooting owns the persisted `log_information` choice:
`essential` (default on every package channel) or `all`. It uses the existing
Preferences schema, atomic storage, and backup format. Older backups that omit
the key resolve to Essential. `CAVEVIEWER_LOG_INFORMATION` supplies the usual
environment fallback when no saved value exists.

The runtime `log_level` default derives from that choice: Essential uses INFO
and All uses DEBUG. Explicit `CAVEVIEWER_LOG_LEVEL` or command-line overrides
retain their existing precedence. File handlers use the resolved threshold,
including for child loggers with their own levels. Essential retains WARNING,
ERROR, and CRITICAL as well as INFO.

Saving the Help choice does not reconfigure the active logging session. The
new threshold applies on restart. A retained Preferences editor synchronizes
this saved field without discarding unrelated staged edits.

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
