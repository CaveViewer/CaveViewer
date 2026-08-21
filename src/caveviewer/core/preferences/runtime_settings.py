"""Core runtime-settings registry and immutable composition snapshots.

This module owns the pure configuration boundary between process inputs and
application services.  It does not read or mutate ``os.environ`` while
resolving a snapshot: the application composition root supplies environment,
saved-preference, command-line, and platform facts explicitly.

``PreferenceSpec`` remains the authority for persisted fields.  Its entries in
this registry reference that schema instead of copying validation rules.  The
remaining entries describe environment-only settings so later caller migrations
can receive one typed :class:`RuntimeSettings` snapshot rather than consulting
the process environment independently.
"""

from __future__ import annotations

import math
import os
import sys
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import TypeAlias

from caveviewer.core.preferences.schema import (
    PREFERENCE_FIELDS,
    PreferenceDefaultContext,
    PreferenceSpec,
    PreferenceValueType,
    validate_preference,
)
from caveviewer.core.release_metadata import (
    ReleaseMetadata,
    default_release_metadata,
    load_embedded_release_metadata,
)
from caveviewer.storage_paths import ApplicationPaths, resolve_application_paths


RuntimeValue: TypeAlias = str | int | float | bool | None
RuntimeSettingParser: TypeAlias = Callable[[str], RuntimeValue]
RuntimeSettingDefault: TypeAlias = (
    RuntimeValue
    | Callable[["RuntimePlatformFacts", Mapping[str, RuntimeValue]], RuntimeValue]
)


@dataclass(frozen=True, slots=True)
class ImportRuntimeSettings:
    """Serializable import settings owned by one spawned import request.

    The parent composition boundary creates this immutable value from its
    resolved snapshot and sends it with a child-process launch request.  The
    child must use these values rather than consulting the parent's mutable
    process environment.
    """

    map_cache_dir: str | None
    chunk_size_meters: float
    max_upload_group_mb: float
    obj_scan_throttle_seconds: float
    obj_import_batch_faces: int
    obj_bucket_workers: int
    chunk_build_workers: int
    chunk_build_reserved_cpus: int
    import_nice_increment: int


@dataclass(frozen=True, slots=True)
class StreamingRuntimeSettings:
    """Immutable worker and residency policy for one streaming-world owner."""

    memory_target_percent: float
    gpu_memory_target_percent: float
    gpu_memory_gb: float | None
    io_workers: int
    io_reserved_cpus: int
    io_nice_increment: int
    texture_resident_cache_mb: float | None
    upload_chunks_per_frame: int
    upload_groups_per_frame: int
    upload_time_budget_ms: float


@dataclass(frozen=True, slots=True)
class RecordingRuntimeSettings:
    """Immutable recording policy consumed by one viewer-session owner."""

    directory: str
    ffmpeg_path: str | None
    fps: int
    max_height: int
    crf: int


@dataclass(frozen=True, slots=True)
class ViewerRuntimeSettings:
    """Immutable GUI/viewer values applied before a window is launched."""

    app_icon: str | None
    force_startup_focus: bool
    gpu_draw_timer: bool
    navigation_guard: bool
    navigation_guard_radius_cells: int
    text_antialiasing_mode: str
    tk_scale: float | None
    ui_font: str | None
    ui_text_scale: float
    ui_text_scale_override: float | None
    viewer_ui_scale: float | None
    vsync: bool
    max_texture_dimension: int | None
    commit_identifier: str | None
    streaming: StreamingRuntimeSettings
    recording: RecordingRuntimeSettings


@dataclass(frozen=True, slots=True)
class MapLibraryRuntimeSettings:
    """Immutable Map Library source and storage values for one UI session."""

    directory: str
    storage_home: str | None
    data_directory: str
    cache_directory: str
    repository: str
    release_tag: str
    api_url: str
    catalog_asset_name: str


class RuntimeSettingCategory(str, Enum):
    """Whether a setting is persisted, environment-only, or launch-only."""

    PERSISTED_PREFERENCE = "persisted_preference"
    ENVIRONMENT = "environment"
    COMMAND_LINE = "command_line"


class RuntimeValueType(str, Enum):
    """The immutable Python value type produced by one registry parser."""

    TEXT = "text"
    INTEGER = "integer"
    FLOAT = "float"
    BOOLEAN = "boolean"


class SettingSource(str, Enum):
    """The input that supplied one resolved runtime value."""

    BUILT_IN = "built_in"
    PREFERENCES = "preferences"
    ENVIRONMENT = "environment"
    CLI = "cli"


class InvalidValuePolicy(str, Enum):
    """How resolution handles a value rejected by a setting parser."""

    FALL_BACK = "fall_back"
    RAISE = "raise"


@dataclass(frozen=True)
class RuntimePlatformFacts:
    """Stable process and package facts supplied by the composition boundary."""

    platform_name: str
    os_name: str
    home: str | os.PathLike[str] | None = None
    release_metadata: ReleaseMetadata = field(default_factory=default_release_metadata)


def current_runtime_platform_facts() -> RuntimePlatformFacts:
    """Capture process facts for legacy composition roots.

    The resolver itself deliberately requires explicit facts.  This small
    convenience factory keeps the one unavoidable process-state read at an
    application edge until all launch paths have been migrated.
    """

    return RuntimePlatformFacts(
        platform_name=sys.platform,
        os_name=os.name,
        home=os.path.expanduser("~"),
        release_metadata=load_embedded_release_metadata(),
    )


@dataclass(frozen=True)
class RuntimeSettingSpec:
    """Declarative metadata and parser for one runtime setting.

    A persisted-preference entry points at its existing ``PreferenceSpec``.
    This avoids a second copy of its range, conversion, and default policy.
    ``default`` is allowed to be ``None`` for an optional override whose
    feature-specific automatic policy is intentionally applied later.
    """

    key: str
    category: RuntimeSettingCategory
    value_type: RuntimeValueType
    environment_variable: str | None
    parser: RuntimeSettingParser
    default: RuntimeSettingDefault
    description: str
    documentation_visible: bool
    diagnostic_safe: bool
    minimum: int | float | None = None
    maximum: int | float | None = None
    enum_values: tuple[str, ...] = ()
    preference: PreferenceSpec | None = None
    legacy_environment_variables: tuple[str, ...] = ()
    cli_name: str | None = None
    empty_value_is_supplied: bool = False
    invalid_value_policy: InvalidValuePolicy = InvalidValuePolicy.FALL_BACK

    def __post_init__(self) -> None:
        if not self.key:
            raise ValueError("runtime setting key must be non-empty")
        if not callable(self.parser):
            raise TypeError("runtime setting parser must be callable")
        if not self.description.strip():
            raise ValueError(f"runtime setting {self.key!r} needs a description")
        if self.preference is not None:
            if self.category is not RuntimeSettingCategory.PERSISTED_PREFERENCE:
                raise ValueError("PreferenceSpec entries must be persisted preferences")
            if self.environment_variable != self.preference.env_var:
                raise ValueError("PreferenceSpec entry must use its schema environment variable")
        elif self.category is RuntimeSettingCategory.PERSISTED_PREFERENCE:
            raise ValueError("persisted preference entries require a PreferenceSpec")
        if (
            self.category is not RuntimeSettingCategory.COMMAND_LINE
            and not self.environment_variable
        ):
            raise ValueError("runtime setting needs an environment variable")
        if self.environment_variable in self.legacy_environment_variables:
            raise ValueError("legacy setting name duplicates the primary name")
        if len(set(self.legacy_environment_variables)) != len(
            self.legacy_environment_variables
        ):
            raise ValueError("legacy environment setting names must be unique")

    @property
    def environment_variables(self) -> tuple[str, ...]:
        """Return the primary variable followed by lower-priority aliases."""

        if self.environment_variable is None:
            return self.legacy_environment_variables
        return (self.environment_variable, *self.legacy_environment_variables)

    def built_in_value(
        self,
        *,
        environ: Mapping[str, str],
        platform: RuntimePlatformFacts,
        resolved_values: Mapping[str, RuntimeValue],
    ) -> RuntimeValue:
        """Return one parsed built-in value without consulting process globals."""

        if self.preference is not None:
            default_context = PreferenceDefaultContext(
                environ=environ,
                platform_name=platform.platform_name,
                home=platform.home,
            )
            return self.parser(self.preference.built_in_default(default_context))

        default = self.default
        value = (
            default(platform, resolved_values)
            if callable(default)
            else default
        )
        return None if value is None else self.parser(str(value))


@dataclass(frozen=True)
class ResolvedRuntimeSetting:
    """Typed effective value and the input source that supplied it."""

    value: RuntimeValue
    source: SettingSource


@dataclass(frozen=True)
class RuntimeSettingIssue:
    """A rejected input retained for the composition boundary to report safely."""

    key: str
    source: SettingSource
    raw_value: str
    message: str


class RuntimeSettingsResolutionError(ValueError):
    """Raised when a setting preserves an existing fail-fast validation rule."""

    def __init__(self, issue: RuntimeSettingIssue) -> None:
        self.issue = issue
        super().__init__(
            f"Invalid runtime setting {issue.key!r} from {issue.source.value}: "
            f"{issue.message}"
        )


@dataclass(frozen=True, eq=False)
class RuntimeSettings(Mapping[str, RuntimeValue]):
    """Immutable runtime configuration snapshot owned by application composition."""

    __hash__ = None
    _entries: Mapping[str, ResolvedRuntimeSetting]
    storage_paths: ApplicationPaths
    issues: tuple[RuntimeSettingIssue, ...] = ()

    def __post_init__(self) -> None:
        entries = dict(self._entries)
        expected_keys = {spec.key for spec in RUNTIME_SETTING_SPECS}
        if set(entries) != expected_keys:
            raise ValueError("RuntimeSettings requires exactly the declared registry keys")
        object.__setattr__(self, "_entries", MappingProxyType(entries))
        object.__setattr__(self, "issues", tuple(self.issues))

    def __getitem__(self, key: str) -> RuntimeValue:
        return self._entries[key].value

    def __iter__(self) -> Iterator[str]:
        return iter(self._entries)

    def __len__(self) -> int:
        return len(self._entries)

    def entry(self, key: str) -> ResolvedRuntimeSetting:
        """Return one value together with its provenance."""

        return self._entries[key]

    def source(self, key: str) -> SettingSource:
        """Return the source that supplied one effective value."""

        return self._entries[key].source

    def as_dict(self) -> dict[str, RuntimeValue]:
        """Return a mutable value copy for serializable worker requests."""

        return {key: entry.value for key, entry in self._entries.items()}

    def import_configuration(self) -> ImportRuntimeSettings:
        """Return the explicit, serializable settings for one import child."""

        return ImportRuntimeSettings(
            map_cache_dir=_optional_runtime_text(self["map_cache_dir"]),
            chunk_size_meters=_runtime_float(self["chunk_size_meters"]),
            max_upload_group_mb=_runtime_float(self["max_upload_group_mb"]),
            obj_scan_throttle_seconds=(
                _runtime_float(self["obj_scan_throttle_ms"]) / 1_000.0
            ),
            obj_import_batch_faces=(
                _runtime_integer(self["obj_import_batch_thousands"]) * 1_000
            ),
            obj_bucket_workers=_runtime_integer(self["obj_bucket_workers"]),
            chunk_build_workers=_runtime_integer(self["chunk_build_workers"]),
            chunk_build_reserved_cpus=_runtime_integer(
                self["chunk_build_reserved_cpus"]
            ),
            import_nice_increment=_runtime_integer(self["import_nice_increment"]),
        )

    def streaming_configuration(self) -> StreamingRuntimeSettings:
        """Return the immutable worker/residency policy for one map session."""

        return StreamingRuntimeSettings(
            memory_target_percent=_runtime_float(self["memory_target_percent"]),
            gpu_memory_target_percent=_runtime_float(
                self["gpu_memory_target_percent"]
            ),
            gpu_memory_gb=_optional_runtime_float(self["gpu_memory_gb"]),
            io_workers=_runtime_integer(self["io_workers"]),
            io_reserved_cpus=_runtime_integer(self["io_reserved_cpus"]),
            io_nice_increment=_runtime_integer(self["io_nice_increment"]),
            texture_resident_cache_mb=_optional_runtime_float(
                self["texture_resident_cache_mb"]
            ),
            upload_chunks_per_frame=_runtime_integer(
                self["upload_chunks_per_frame"]
            ),
            upload_groups_per_frame=_runtime_integer(
                self["upload_groups_per_frame"]
            ),
            upload_time_budget_ms=_runtime_float(self["upload_time_budget_ms"]),
        )

    def viewer_configuration(self) -> ViewerRuntimeSettings:
        """Return one viewer-owned bundle without exposing process globals."""

        return ViewerRuntimeSettings(
            app_icon=_optional_runtime_text(self["app_icon"]),
            force_startup_focus=_runtime_boolean(self["force_startup_focus"]),
            gpu_draw_timer=_runtime_boolean(self["gpu_draw_timer"]),
            navigation_guard=_runtime_boolean(self["navigation_guard"]),
            navigation_guard_radius_cells=_runtime_integer(
                self["navigation_guard_radius_cells"]
            ),
            text_antialiasing_mode=_runtime_text(self["text_antialiasing_mode"]),
            tk_scale=_optional_runtime_float(self["tk_scale"]),
            ui_font=_optional_runtime_text(self["ui_font"]),
            ui_text_scale=_runtime_float(self["ui_text_scale"]),
            ui_text_scale_override=(
                _runtime_float(self["ui_text_scale"])
                if self.source("ui_text_scale") is not SettingSource.BUILT_IN
                else None
            ),
            viewer_ui_scale=_optional_runtime_float(self["viewer_ui_scale"]),
            vsync=_runtime_boolean(self["vsync"]),
            max_texture_dimension=_optional_runtime_integer(
                self["max_texture_size"]
            ),
            commit_identifier=_optional_runtime_text(self["commit_identifier"]),
            streaming=self.streaming_configuration(),
            recording=RecordingRuntimeSettings(
                directory=_runtime_text(self["recording_dir"]),
                ffmpeg_path=_optional_runtime_text(self["ffmpeg_path"]),
                fps=_runtime_integer(self["recording_fps"]),
                max_height=_runtime_integer(self["recording_max_height"]),
                crf=_runtime_integer(self["recording_crf"]),
            ),
        )

    def map_library_configuration(self) -> MapLibraryRuntimeSettings:
        """Return the Map Library's source and storage configuration."""

        return MapLibraryRuntimeSettings(
            directory=_runtime_text(self["map_library_dir"]),
            storage_home=_optional_runtime_text(self["storage_home"]),
            data_directory=str(self.storage_paths.data_dir),
            cache_directory=str(self.storage_paths.cache_dir),
            repository=_runtime_text(self["map_library_repository"]),
            release_tag=_runtime_text(self["map_library_release_tag"]),
            api_url=_runtime_text(self["map_library_api_url"]),
            catalog_asset_name=_runtime_text(
                self["map_library_catalog_asset_name"]
            ),
        )


def _runtime_text(value: RuntimeValue) -> str:
    if not isinstance(value, str):
        raise TypeError(f"Expected resolved text value, got {value!r}")
    return value


def _optional_runtime_text(value: RuntimeValue) -> str | None:
    if value is None:
        return None
    return _runtime_text(value)


def _runtime_integer(value: RuntimeValue) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"Expected resolved integer value, got {value!r}")
    return value


def _runtime_float(value: RuntimeValue) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"Expected resolved floating-point value, got {value!r}")
    return float(value)


def _optional_runtime_integer(value: RuntimeValue) -> int | None:
    if value is None:
        return None
    return _runtime_integer(value)


def _optional_runtime_float(value: RuntimeValue) -> float | None:
    if value is None:
        return None
    return _runtime_float(value)


def _runtime_boolean(value: RuntimeValue) -> bool:
    if not isinstance(value, bool):
        raise TypeError(f"Expected resolved boolean value, got {value!r}")
    return value


def _required_text(raw_value: str) -> str:
    value = str(raw_value).strip()
    if not value:
        raise ValueError("a non-empty value is required")
    return value


def _optional_text(raw_value: str) -> str | None:
    value = str(raw_value).strip()
    return value or None


def _optional_override_text(raw_value: str) -> str:
    """Keep an explicitly empty override distinct from an unset variable."""

    return str(raw_value).strip()


def _boolean(raw_value: str) -> bool:
    value = str(raw_value).strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise ValueError("expected one of 1, 0, true, false, yes, no, on, or off")


def _integer(
    *,
    minimum: int | None = None,
    maximum: int | None = None,
    clamp: bool = False,
) -> RuntimeSettingParser:
    def parse(raw_value: str) -> int:
        try:
            value = int(str(raw_value).strip())
        except (TypeError, ValueError) as exc:
            raise ValueError("expected a whole number") from exc
        if clamp:
            if minimum is not None:
                value = max(minimum, value)
            if maximum is not None:
                value = min(maximum, value)
            return value
        if minimum is not None and value < minimum:
            raise ValueError(f"expected a value no lower than {minimum}")
        if maximum is not None and value > maximum:
            raise ValueError(f"expected a value no higher than {maximum}")
        return value

    return parse


def _floating(
    *,
    minimum: float | None = None,
    maximum: float | None = None,
    clamp: bool = False,
) -> RuntimeSettingParser:
    def parse(raw_value: str) -> float:
        try:
            value = float(str(raw_value).strip())
        except (TypeError, ValueError) as exc:
            raise ValueError("expected a number") from exc
        if not math.isfinite(value):
            raise ValueError("expected a finite number")
        if clamp:
            if minimum is not None:
                value = max(minimum, value)
            if maximum is not None:
                value = min(maximum, value)
            return value
        if minimum is not None and value < minimum:
            raise ValueError(f"expected a value no lower than {minimum:g}")
        if maximum is not None and value > maximum:
            raise ValueError(f"expected a value no higher than {maximum:g}")
        return value

    return parse


def _nonnegative_integer(raw_value: str) -> int:
    """Match worker-priority policy: negative values disable the adjustment."""

    try:
        return max(0, int(str(raw_value).strip()))
    except (TypeError, ValueError) as exc:
        raise ValueError("expected a whole number") from exc


def _positive_bounded_integer(maximum: int) -> RuntimeSettingParser:
    """Accept positive values and retain the existing upper-bound clamp."""

    def parse(raw_value: str) -> int:
        try:
            value = int(str(raw_value).strip())
        except (TypeError, ValueError) as exc:
            raise ValueError("expected a whole number") from exc
        if value <= 0:
            raise ValueError("expected a positive whole number")
        return min(maximum, value)

    return parse


def _texture_dimension(raw_value: str) -> int:
    """Match the texture decoder's float-to-integer dimension coercion."""

    try:
        value = int(float(str(raw_value).strip()))
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("expected a texture dimension") from exc
    return max(512, min(16384, value))


def _optional_positive_float(raw_value: str) -> float | None:
    text = str(raw_value).strip()
    if not text:
        return None
    value = _floating(minimum=0.0)(text)
    if value <= 0.0:
        raise ValueError("expected a positive value")
    return value


def _enum(*values: str) -> RuntimeSettingParser:
    allowed = frozenset(values)

    def parse(raw_value: str) -> str:
        value = str(raw_value).strip().lower()
        if value not in allowed:
            choices = ", ".join(sorted(allowed))
            raise ValueError(f"expected one of: {choices}")
        return value

    return parse


def _text_antialiasing_mode(raw_value: str) -> str:
    """Preserve the existing unknown-value route to normal FreeType hinting."""

    value = str(raw_value).strip().lower()
    return value if value in {"lcd", "light", "normal"} else "normal"


def _log_level(raw_value: str) -> str:
    value = str(raw_value).strip().upper()
    return value if value in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"} else "INFO"


def _vsync(raw_value: str) -> bool:
    """Preserve the current viewer rule: only explicit false tokens disable it."""

    return str(raw_value).strip().lower() not in {"0", "false", "no"}


def _preference_parser(field: PreferenceSpec) -> RuntimeSettingParser:
    def parse(raw_value: str) -> RuntimeValue:
        result = validate_preference(field, raw_value)
        if not result.is_valid:
            raise ValueError(result.message or "invalid preference value")
        normalized = result.normalized_value
        if field.optional and not normalized:
            return None
        if field.value_type is PreferenceValueType.INT:
            return int(normalized)
        if field.value_type is PreferenceValueType.FLOAT:
            return float(normalized)
        return normalized

    return parse


def _preference_setting(field: PreferenceSpec) -> RuntimeSettingSpec:
    value_type = (
        RuntimeValueType.INTEGER
        if field.value_type is PreferenceValueType.INT
        else (
            RuntimeValueType.FLOAT
            if field.value_type is PreferenceValueType.FLOAT
            else RuntimeValueType.TEXT
        )
    )
    return RuntimeSettingSpec(
        key=field.key,
        category=RuntimeSettingCategory.PERSISTED_PREFERENCE,
        value_type=value_type,
        environment_variable=field.env_var,
        parser=_preference_parser(field),
        default=None,
        description=field.hint,
        documentation_visible=True,
        diagnostic_safe=False,
        minimum=field.minimum,
        maximum=field.maximum,
        preference=field,
    )


def _environment_setting(
    key: str,
    environment_variable: str,
    description: str,
    *,
    value_type: RuntimeValueType,
    parser: RuntimeSettingParser,
    default: RuntimeSettingDefault,
    documentation_visible: bool = True,
    diagnostic_safe: bool = True,
    minimum: int | float | None = None,
    maximum: int | float | None = None,
    enum_values: tuple[str, ...] = (),
    legacy_environment_variables: tuple[str, ...] = (),
    cli_name: str | None = None,
    empty_value_is_supplied: bool = False,
    invalid_value_policy: InvalidValuePolicy = InvalidValuePolicy.FALL_BACK,
) -> RuntimeSettingSpec:
    return RuntimeSettingSpec(
        key=key,
        category=RuntimeSettingCategory.ENVIRONMENT,
        value_type=value_type,
        environment_variable=environment_variable,
        parser=parser,
        default=default,
        description=description,
        documentation_visible=documentation_visible,
        diagnostic_safe=diagnostic_safe,
        minimum=minimum,
        maximum=maximum,
        enum_values=enum_values,
        legacy_environment_variables=legacy_environment_variables,
        cli_name=cli_name,
        empty_value_is_supplied=empty_value_is_supplied,
        invalid_value_policy=invalid_value_policy,
    )


def _platform_text_antialiasing_default(
    platform: RuntimePlatformFacts,
    _resolved_values: Mapping[str, RuntimeValue],
) -> str:
    if platform.platform_name == "darwin" or platform.platform_name.startswith(
        "linux"
    ):
        return "light"
    return "normal"


def _map_library_api_default(
    _platform: RuntimePlatformFacts,
    resolved_values: Mapping[str, RuntimeValue],
) -> str:
    repository = resolved_values["map_library_repository"]
    release_tag = resolved_values["map_library_release_tag"]
    return (
        "https://api.github.com/repos/"
        f"{repository}/releases/tags/{release_tag}"
    )


def _embedded_update_channel_default(
    platform: RuntimePlatformFacts,
    _resolved_values: Mapping[str, RuntimeValue],
) -> str:
    """Use the package-selected channel when no local override is supplied."""

    return platform.release_metadata.release_channel


RUNTIME_SETTING_SPECS = (
    *(_preference_setting(field) for field in PREFERENCE_FIELDS),
    _environment_setting(
        "storage_home",
        "CAVEVIEWER_HOME",
        "Optional absolute portable storage root.",
        value_type=RuntimeValueType.TEXT,
        parser=_optional_text,
        default=None,
        diagnostic_safe=False,
    ),
    _environment_setting(
        "map_cache_dir",
        "CAVEVIEWER_MAP_CACHE_DIR",
        "Optional absolute root for generated map caches.",
        value_type=RuntimeValueType.TEXT,
        parser=_optional_text,
        default=None,
        diagnostic_safe=False,
    ),
    _environment_setting(
        "app_icon",
        "CAVEVIEWER_APP_ICON",
        "Optional custom application icon path.",
        value_type=RuntimeValueType.TEXT,
        parser=_optional_text,
        default=None,
        diagnostic_safe=False,
    ),
    _environment_setting(
        "log_level",
        "CAVEVIEWER_LOG_LEVEL",
        "Application logging verbosity.",
        value_type=RuntimeValueType.TEXT,
        parser=_log_level,
        default="INFO",
        enum_values=("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"),
        cli_name="log_level",
    ),
    _environment_setting(
        "force_startup_focus",
        "CAVEVIEWER_FORCE_STARTUP_FOCUS",
        "Whether a viewer may request foreground focus at startup.",
        value_type=RuntimeValueType.BOOLEAN,
        parser=_boolean,
        default=False,
    ),
    _environment_setting(
        "force_update",
        "CAVEVIEWER_FORCE_UPDATE",
        "Whether update presentation is forced for local testing.",
        value_type=RuntimeValueType.BOOLEAN,
        parser=_boolean,
        default=False,
        cli_name="force_update",
    ),
    _environment_setting(
        "github_repository",
        "CAVEVIEWER_GITHUB_REPO",
        "GitHub owner/repository used for update configuration.",
        value_type=RuntimeValueType.TEXT,
        parser=_required_text,
        default="CaveViewer/CaveViewer",
    ),
    _environment_setting(
        "update_branch",
        "CAVEVIEWER_UPDATE_BRANCH",
        "Git branch used to derive the default update manifest URL.",
        value_type=RuntimeValueType.TEXT,
        parser=_required_text,
        default="main",
        cli_name="update_branch",
    ),
    _environment_setting(
        "update_channel",
        "CAVEVIEWER_UPDATE_CHANNEL",
        "Update manifest channel; defaults to the channel embedded in the package.",
        value_type=RuntimeValueType.TEXT,
        parser=_enum("stable", "prerelease"),
        default=_embedded_update_channel_default,
        enum_values=("stable", "prerelease"),
    ),
    _environment_setting(
        "update_manifest_url",
        "CAVEVIEWER_UPDATE_MANIFEST_URL",
        "Optional full URL overriding the platform-derived update manifest.",
        value_type=RuntimeValueType.TEXT,
        parser=_optional_override_text,
        default=None,
        diagnostic_safe=False,
        empty_value_is_supplied=True,
    ),
    _environment_setting(
        "update_manifest_signature_url",
        "CAVEVIEWER_UPDATE_MANIFEST_SIGNATURE_URL",
        "Optional full URL overriding the update-manifest signature location.",
        value_type=RuntimeValueType.TEXT,
        parser=_optional_override_text,
        default=None,
        diagnostic_safe=False,
        empty_value_is_supplied=True,
    ),
    _environment_setting(
        "gpu_draw_timer",
        "CAVEVIEWER_GPU_DRAW_TIMER",
        "Enable GPU draw timing for diagnostics.",
        value_type=RuntimeValueType.BOOLEAN,
        parser=_boolean,
        default=False,
        documentation_visible=False,
    ),
    _environment_setting(
        "import_nice_increment",
        "CAVEVIEWER_IMPORT_NICE",
        "Best-effort positive niceness increment for import worker processes.",
        value_type=RuntimeValueType.INTEGER,
        parser=_integer(),
        default=5,
    ),
    _environment_setting(
        "io_nice_increment",
        "CAVEVIEWER_IO_NICE",
        "Best-effort positive niceness increment for streaming workers.",
        value_type=RuntimeValueType.INTEGER,
        parser=_nonnegative_integer,
        default=5,
        minimum=0,
    ),
    _environment_setting(
        "obj_bucket_workers",
        "CAVEVIEWER_OBJ_BUCKET_WORKERS",
        "Maximum worker count for incremental OBJ bucket preparation.",
        value_type=RuntimeValueType.INTEGER,
        parser=_positive_bounded_integer(32),
        default=2,
        minimum=1,
        maximum=32,
    ),
    _environment_setting(
        "max_texture_size",
        "CAVEVIEWER_MAX_TEXTURE_SIZE",
        "Optional maximum texture dimension in pixels before decode.",
        value_type=RuntimeValueType.INTEGER,
        parser=_texture_dimension,
        default=None,
        minimum=512,
        maximum=16384,
    ),
    _environment_setting(
        "navigation_guard",
        "CAVEVIEWER_NAVIGATION_GUARD",
        "Keep free-fly navigation near occupied map chunks.",
        value_type=RuntimeValueType.BOOLEAN,
        parser=_boolean,
        default=True,
    ),
    _environment_setting(
        "navigation_guard_radius_cells",
        "CAVEVIEWER_NAVIGATION_GUARD_RADIUS_CELLS",
        "Number of cells around occupied map chunks that remain navigable.",
        value_type=RuntimeValueType.INTEGER,
        parser=_integer(minimum=0, maximum=12, clamp=True),
        default=2,
        minimum=0,
        maximum=12,
    ),
    _environment_setting(
        "ffmpeg_path",
        "CAVEVIEWER_FFMPEG",
        "Optional explicit ffmpeg executable used by recording.",
        value_type=RuntimeValueType.TEXT,
        parser=_optional_text,
        default=None,
        diagnostic_safe=False,
    ),
    _environment_setting(
        "recording_fps",
        "CAVEVIEWER_RECORDING_FPS",
        "Target MP4 recording frame rate.",
        value_type=RuntimeValueType.INTEGER,
        parser=_integer(minimum=1, maximum=60, clamp=True),
        default=30,
        minimum=1,
        maximum=60,
    ),
    _environment_setting(
        "recording_max_height",
        "CAVEVIEWER_RECORDING_MAX_HEIGHT",
        "Maximum encoded recording height in pixels.",
        value_type=RuntimeValueType.INTEGER,
        parser=_integer(minimum=240, maximum=4320, clamp=True),
        default=720,
        minimum=240,
        maximum=4320,
    ),
    _environment_setting(
        "recording_crf",
        "CAVEVIEWER_RECORDING_CRF",
        "H.264 recording quality value.",
        value_type=RuntimeValueType.INTEGER,
        parser=_integer(minimum=0, maximum=51, clamp=True),
        default=23,
        minimum=0,
        maximum=51,
    ),
    _environment_setting(
        "texture_resident_cache_mb",
        "CAVEVIEWER_TEXTURE_RESIDENT_CACHE_MB",
        "Optional resident GPU texture-cache cap in MiB.",
        value_type=RuntimeValueType.FLOAT,
        parser=_optional_positive_float,
        default=None,
        minimum=0,
    ),
    _environment_setting(
        "text_antialiasing_mode",
        "CAVEVIEWER_TEXT_AA_MODE",
        "FreeType text anti-aliasing mode.",
        value_type=RuntimeValueType.TEXT,
        parser=_text_antialiasing_mode,
        default=_platform_text_antialiasing_default,
        enum_values=("lcd", "light", "normal"),
    ),
    _environment_setting(
        "tk_scale",
        "CAVEVIEWER_TK_SCALE",
        "Optional Tk display scaling override.",
        value_type=RuntimeValueType.FLOAT,
        parser=_floating(minimum=0.75, maximum=4.0, clamp=True),
        default=None,
        minimum=0.75,
        maximum=4.0,
    ),
    _environment_setting(
        "ui_font",
        "CAVEVIEWER_UI_FONT",
        "Optional font path for the OpenGL text renderer.",
        value_type=RuntimeValueType.TEXT,
        parser=_optional_text,
        default=None,
        diagnostic_safe=False,
    ),
    _environment_setting(
        "ui_text_scale",
        "CAVEVIEWER_UI_TEXT_SCALE",
        "Base scale for FreeType-rendered viewer overlay text.",
        value_type=RuntimeValueType.FLOAT,
        parser=_floating(minimum=0.01),
        default=1.28,
        minimum=0.01,
    ),
    _environment_setting(
        "viewer_ui_scale",
        "CAVEVIEWER_VIEWER_UI_SCALE",
        "Optional viewer HUD scale; unset keeps automatic sizing.",
        value_type=RuntimeValueType.FLOAT,
        parser=_floating(minimum=0.75, maximum=2.0, clamp=True),
        default=None,
        minimum=0.75,
        maximum=2.0,
    ),
    _environment_setting(
        "vsync",
        "CAVEVIEWER_VSYNC",
        "Whether the viewer waits for vertical sync.",
        value_type=RuntimeValueType.BOOLEAN,
        parser=_vsync,
        default=True,
        enum_values=("0", "1", "false", "true", "no", "yes"),
        cli_name="vsync",
    ),
    _environment_setting(
        "window_system",
        "CAVEVIEWER_WINDOW_SYSTEM",
        "Requested Linux viewer window-system route.",
        value_type=RuntimeValueType.TEXT,
        parser=_enum("auto", "x11", "wayland"),
        default="auto",
        enum_values=("auto", "x11", "wayland"),
        invalid_value_policy=InvalidValuePolicy.RAISE,
    ),
    _environment_setting(
        "map_library_repository",
        "CAVEVIEWER_MAP_LIBRARY_REPO",
        "GitHub owner/repository used by the Map Library source.",
        value_type=RuntimeValueType.TEXT,
        parser=_required_text,
        default="CaveViewer/CaveViewer",
        legacy_environment_variables=("CAVEVIEWER_SAMPLE_MAPS_REPO",),
    ),
    _environment_setting(
        "map_library_release_tag",
        "CAVEVIEWER_MAP_LIBRARY_RELEASE_TAG",
        "Release tag used by the Map Library source.",
        value_type=RuntimeValueType.TEXT,
        parser=_required_text,
        default="sample-data",
        legacy_environment_variables=("CAVEVIEWER_SAMPLE_DATA_TAG",),
    ),
    _environment_setting(
        "map_library_api_url",
        "CAVEVIEWER_MAP_LIBRARY_API_URL",
        "GitHub release API URL used by the Map Library source.",
        value_type=RuntimeValueType.TEXT,
        parser=_required_text,
        default=_map_library_api_default,
        legacy_environment_variables=("CAVEVIEWER_SAMPLE_MAPS_API_URL",),
        diagnostic_safe=False,
    ),
    _environment_setting(
        "map_library_catalog_asset_name",
        "CAVEVIEWER_MAP_LIBRARY_CATALOG_ASSET_NAME",
        "Catalog asset name used by the Map Library source.",
        value_type=RuntimeValueType.TEXT,
        parser=_required_text,
        default="caveviewer-map-library.v1.json",
    ),
    _environment_setting(
        "commit_identifier",
        "CAVEVIEWER_COMMIT",
        "Optional source revision recorded by local benchmark runs.",
        value_type=RuntimeValueType.TEXT,
        parser=_optional_text,
        default=None,
        documentation_visible=False,
    ),
)


def _registry_by_key() -> Mapping[str, RuntimeSettingSpec]:
    specs = {spec.key: spec for spec in RUNTIME_SETTING_SPECS}
    if len(specs) != len(RUNTIME_SETTING_SPECS):
        raise RuntimeError("runtime setting keys must be unique")
    return MappingProxyType(specs)


def _registry_by_environment_variable() -> Mapping[str, RuntimeSettingSpec]:
    specs: dict[str, RuntimeSettingSpec] = {}
    for spec in RUNTIME_SETTING_SPECS:
        for variable in spec.environment_variables:
            if variable in specs:
                raise RuntimeError("runtime environment variable names must be unique")
            specs[variable] = spec
    return MappingProxyType(specs)


RUNTIME_SETTING_SPECS_BY_KEY = _registry_by_key()
RUNTIME_SETTING_SPECS_BY_ENVIRONMENT_VARIABLE = _registry_by_environment_variable()

# These variables configure repository tooling or packaging shells.  They are
# intentionally inventoried here so a future diagnostic migration cannot leak
# them into the application-runtime snapshot by accident.
PACKAGING_OR_DEVELOPMENT_ENVIRONMENT_VARIABLES = frozenset(
    {
        "CAVEVIEWER_DEV_VENV",
        "CAVEVIEWER_LINUX_BUILD_VENV",
        "CAVEVIEWER_MACOS_BUILD_VENV",
        "CAVEVIEWER_BUILD_RELEASE_CHANNEL",
        "CAVEVIEWER_RELEASE_METADATA_PATH",
        "CAVEVIEWER_PROJECT_ROOT",
    }
)


def runtime_setting_spec(name: str) -> RuntimeSettingSpec:
    """Look up one declaration by internal key or supported environment name."""

    try:
        return RUNTIME_SETTING_SPECS_BY_KEY[name]
    except KeyError:
        return RUNTIME_SETTING_SPECS_BY_ENVIRONMENT_VARIABLE[name]


def runtime_environment_variable_names() -> frozenset[str]:
    """Return every application-runtime environment name, including aliases."""

    return frozenset(RUNTIME_SETTING_SPECS_BY_ENVIRONMENT_VARIABLE)


def render_runtime_environment_table() -> str:
    """Render the deterministic runtime-variable reference for source docs.

    ``docs/development/source-setup.md`` embeds this exact output between
    stable markers.  The renderer intentionally uses declaration metadata
    rather than resolving values, so generating documentation cannot read
    process globals or create platform-dependent output.
    """

    rows = (
        "| Variable | Category | Default | Description |",
        "| --- | --- | --- | --- |",
    )
    rendered_rows = [*rows]
    for spec in RUNTIME_SETTING_SPECS:
        if not spec.documentation_visible or spec.environment_variable is None:
            continue
        variables = " / ".join(
            f"`{name}`" for name in spec.environment_variables
        )
        category = spec.category.value.replace("_", " ")
        default = _runtime_setting_documentation_default(spec)
        description = " ".join(spec.description.split()).replace("|", "\\|")
        rendered_rows.append(
            f"| {variables} | {category} | {default} | {description} |"
        )
    return "\n".join(rendered_rows)


def _runtime_setting_documentation_default(spec: RuntimeSettingSpec) -> str:
    """Return a platform-independent documentation label for one default."""

    if spec.preference is not None:
        return "saved preference or platform default"
    if spec.default is None:
        return "_(unset)_"
    if callable(spec.default):
        return "derived from runtime inputs"
    return f"`{spec.default}`"


def _is_supplied(raw_value: object | None) -> bool:
    return raw_value is not None and bool(str(raw_value).strip())


def _environment_candidates(
    spec: RuntimeSettingSpec,
    environ: Mapping[str, str],
) -> Iterator[str]:
    for variable in spec.environment_variables:
        if variable not in environ:
            continue
        raw_value = environ[variable]
        if _is_supplied(raw_value) or spec.empty_value_is_supplied:
            yield str(raw_value).strip()


def _cli_candidate(
    spec: RuntimeSettingSpec,
    cli_overrides: Mapping[str, object],
) -> str | None:
    if spec.cli_name is None:
        return None
    raw_value = cli_overrides.get(spec.cli_name)
    return str(raw_value).strip() if _is_supplied(raw_value) else None


def _parse_value(
    spec: RuntimeSettingSpec,
    raw_value: str,
    source: SettingSource,
) -> RuntimeValue:
    if spec.preference is not None and source is SettingSource.ENVIRONMENT:
        raw_value = spec.preference.value_from_env(raw_value)
    return spec.parser(raw_value)


def _resolve_candidate(
    spec: RuntimeSettingSpec,
    raw_value: str,
    source: SettingSource,
    issues: list[RuntimeSettingIssue],
) -> RuntimeValue | None:
    try:
        return _parse_value(spec, raw_value, source)
    except (TypeError, ValueError) as exc:
        issue = RuntimeSettingIssue(
            key=spec.key,
            source=source,
            raw_value=raw_value,
            message=str(exc),
        )
        if spec.invalid_value_policy is InvalidValuePolicy.RAISE:
            raise RuntimeSettingsResolutionError(issue) from exc
        issues.append(issue)
        return None


def resolve_runtime_settings(
    *,
    preferences: Mapping[str, object] | None = None,
    environ: Mapping[str, str],
    cli_overrides: Mapping[str, object] | None = None,
    platform: RuntimePlatformFacts,
) -> RuntimeSettings:
    """Compose one typed immutable runtime snapshot from explicit inputs.

    ``preferences`` should be the raw persisted field mapping when original
    provenance matters. A fully resolved ``Preferences`` mapping remains
    accepted for compatibility, but its already-filled values can only be
    attributed to that supplied preference snapshot.

    Persisted settings preserve the current precedence exactly: a valid saved
    preference wins, then a valid environment value, then its built-in default.
    Environment-only settings use a declared command-line override first when
    one exists, then the primary variable, legacy aliases, and finally the
    built-in value.  Invalid fall-back settings are retained as issues rather
    than logged here, leaving diagnostics ownership at the composition edge.
    """

    persisted_values = preferences if preferences is not None else {}
    command_line_values = cli_overrides if cli_overrides is not None else {}
    entries: dict[str, ResolvedRuntimeSetting] = {}
    issues: list[RuntimeSettingIssue] = []

    for spec in RUNTIME_SETTING_SPECS:
        if spec.preference is not None:
            raw_preference = persisted_values.get(spec.preference.key)
            if _is_supplied(raw_preference):
                value = _resolve_candidate(
                    spec,
                    str(raw_preference).strip(),
                    SettingSource.PREFERENCES,
                    issues,
                )
                if value is not None:
                    entries[spec.key] = ResolvedRuntimeSetting(
                        value=value,
                        source=SettingSource.PREFERENCES,
                    )
                    continue
        else:
            raw_cli_value = _cli_candidate(spec, command_line_values)
            if raw_cli_value is not None:
                value = _resolve_candidate(
                    spec,
                    raw_cli_value,
                    SettingSource.CLI,
                    issues,
                )
                if value is not None:
                    entries[spec.key] = ResolvedRuntimeSetting(
                        value=value,
                        source=SettingSource.CLI,
                    )
                    continue

        resolved_from_environment = False
        for raw_environment_value in _environment_candidates(spec, environ):
            value = _resolve_candidate(
                spec,
                raw_environment_value,
                SettingSource.ENVIRONMENT,
                issues,
            )
            if value is None:
                continue
            entries[spec.key] = ResolvedRuntimeSetting(
                value=value,
                source=SettingSource.ENVIRONMENT,
            )
            resolved_from_environment = True
            break
        if resolved_from_environment:
            continue

        try:
            built_in_value = spec.built_in_value(
                environ=environ,
                platform=platform,
                resolved_values={key: entry.value for key, entry in entries.items()},
            )
        except (TypeError, ValueError) as exc:
            raise RuntimeError(
                f"Invalid built-in runtime default for {spec.key}: {exc}"
            ) from exc
        entries[spec.key] = ResolvedRuntimeSetting(
            value=built_in_value,
            source=SettingSource.BUILT_IN,
        )

    storage_paths = resolve_application_paths(
        environ=environ,
        home=platform.home,
        platform_name=platform.platform_name,
    )
    return RuntimeSettings(
        entries,
        storage_paths=storage_paths,
        issues=tuple(issues),
    )


class RuntimeSettingsSession:
    """Application-owned replacement point for immutable runtime snapshots.

    The composition/Tk owner creates this object with a copy of the process
    inputs once at startup.  Calling :meth:`replace_preferences` after a
    successful Preferences save creates a new immutable snapshot for later
    Map Library or viewer work; it never mutates ``os.environ`` and is not a
    cross-thread synchronization primitive.
    """

    def __init__(
        self,
        *,
        preferences: Mapping[str, object] | None,
        environ: Mapping[str, str],
        cli_overrides: Mapping[str, object] | None,
        platform: RuntimePlatformFacts,
    ) -> None:
        self._preferences = dict(preferences or {})
        self._environ = MappingProxyType(dict(environ))
        self._cli_overrides = MappingProxyType(dict(cli_overrides or {}))
        self._platform = platform
        self._snapshot = self._resolve()

    @property
    def snapshot(self) -> RuntimeSettings:
        """Return the current immutable snapshot for the next owned action."""

        return self._snapshot

    def replace_preferences(
        self,
        preferences: Mapping[str, object],
    ) -> RuntimeSettings:
        """Resolve and publish a new snapshot after a successful save."""

        self._preferences = dict(preferences)
        self._snapshot = self._resolve()
        return self._snapshot

    def _resolve(self) -> RuntimeSettings:
        return resolve_runtime_settings(
            preferences=self._preferences,
            environ=self._environ,
            cli_overrides=self._cli_overrides,
            platform=self._platform,
        )
