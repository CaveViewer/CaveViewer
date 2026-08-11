"""Core preference schema, validation, defaults, and env conversion.

This module is intentionally UI-independent. GUI code may present these fields
with dialogs and persistence, but core code can validate import/runtime settings
without importing ``caveviewer.gui``.
"""

from __future__ import annotations

import math
import os
import sys
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import Callable

from caveviewer.core.diagnostics.logging import get_logger
from caveviewer.storage_paths import default_downloads_dir


_LOG = get_logger("PreferencesSchema")


class PreferenceValueType(str, Enum):
    INT = "int"
    FLOAT = "float"
    PATH = "path"
    PATH_CREATE = "path_create"


PreferenceDefaultProvider = str | Callable[[], str]
PreferenceEnvConverter = Callable[[str], str]


@dataclass(frozen=True)
class PreferenceSpec:
    section: str
    key: str
    env_var: str
    label: str
    hint: str
    value_type: PreferenceValueType
    default: PreferenceDefaultProvider
    optional: bool = False
    minimum: float | int | None = None
    maximum: float | int | None = None
    units: str = ""
    env_to_preference: PreferenceEnvConverter | None = None
    preference_to_env: PreferenceEnvConverter | None = None

    def built_in_default(self) -> str:
        value = self.default() if callable(self.default) else self.default
        return str(value).strip()

    def value_from_env(self, raw_value: str) -> str:
        value = str(raw_value).strip()
        if self.env_to_preference is None:
            return value
        return self.env_to_preference(value)

    def value_to_env(self, preference_value: str) -> str:
        value = str(preference_value).strip()
        if self.preference_to_env is None:
            return value
        return self.preference_to_env(value)


@dataclass(frozen=True)
class PreferenceFieldValidationResult:
    is_valid: bool
    message: str | None
    normalized_value: str


@dataclass(frozen=True, eq=False)
class Preferences(Mapping[str, str]):
    """Immutable validated preference snapshot."""

    __hash__ = None
    _values: Mapping[str, str]

    def __post_init__(self) -> None:
        raw_values = dict(self._values)
        expected_keys = {field.key for field in PREFERENCE_FIELDS}
        if set(raw_values) != expected_keys:
            raise ValueError(
                "Preferences requires exactly the declared schema keys"
            )

        normalized: dict[str, str] = {}
        for field in PREFERENCE_FIELDS:
            result = validate_preference(field, raw_values[field.key])
            if not result.is_valid:
                raise ValueError(result.message or f"Invalid value for {field.key}")
            normalized[field.key] = result.normalized_value
        object.__setattr__(self, "_values", MappingProxyType(normalized))

    def __getitem__(self, key: str) -> str:
        return self._values[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Mapping) and dict(self) == dict(other)

    def as_dict(self) -> dict[str, str]:
        return dict(self._values)


@dataclass(frozen=True)
class PreferencesValidationResult:
    is_valid: bool
    message: str | None
    normalized_values: Mapping[str, str]
    error_key: str | None
    preferences: Preferences | None


class PreferencesValidationError(ValueError):
    def __init__(self, result: PreferencesValidationResult) -> None:
        self.result = result
        super().__init__(result.message or "Invalid preferences.")


def _recording_directory_default() -> str:
    return os.path.abspath(
        os.path.expanduser(os.path.join("~", "Movies", "CaveViewer"))
    )


def _map_library_directory_default() -> str:
    return str(default_downloads_dir())


def _scan_throttle_default() -> str:
    return "1" if sys.platform.startswith("win") else "0"


def _faces_env_to_thousands(raw_value: str) -> str:
    face_count = int(str(raw_value).strip())
    if not 1_000 <= face_count <= 2_000_000:
        raise ValueError("face count must be between 1,000 and 2,000,000")
    return str(max(1, round(face_count / 1000)))


def _thousands_to_faces_env(raw_value: str) -> str:
    return str(int(str(raw_value).strip()) * 1000)


PREFERENCE_FIELDS = (
    PreferenceSpec(
        section="streaming",
        key="memory_target_percent",
        env_var="CAVEVIEWER_MEMORY_UTILIZATION_TARGET",
        label="System RAM target",
        hint="Target percent of available RAM for loaded chunks.",
        value_type=PreferenceValueType.FLOAT,
        default="8",
        minimum=1.0,
        maximum=80.0,
        units="percent",
    ),
    PreferenceSpec(
        section="streaming",
        key="gpu_memory_target_percent",
        env_var="CAVEVIEWER_GPU_MEMORY_UTILIZATION_TARGET",
        label="GPU memory target",
        hint="Target percent of GPU memory for texture and geometry residency.",
        value_type=PreferenceValueType.FLOAT,
        default="70",
        minimum=1.0,
        maximum=80.0,
        units="percent",
    ),
    PreferenceSpec(
        section="streaming",
        key="gpu_memory_gb",
        env_var="CAVEVIEWER_GPU_MEMORY_GB",
        label="GPU memory override",
        hint="Manual GPU memory ceiling in GB.",
        value_type=PreferenceValueType.FLOAT,
        default="",
        optional=True,
        minimum=0.5,
        maximum=50.0,
        units="GB",
    ),
    PreferenceSpec(
        section="streaming",
        key="io_workers",
        env_var="CAVEVIEWER_IO_WORKERS",
        label="Loading worker limit",
        hint="Max chunk-loading worker threads.",
        value_type=PreferenceValueType.INT,
        default="2",
        minimum=1,
        maximum=32,
        units="workers",
    ),
    PreferenceSpec(
        section="streaming",
        key="io_reserved_cpus",
        env_var="CAVEVIEWER_IO_RESERVED_CPUS",
        label="Loading CPUs to keep free",
        hint="Logical CPUs reserved from loading.",
        value_type=PreferenceValueType.INT,
        default="3",
        minimum=2,
        maximum=32,
        units="logical CPUs",
    ),
    PreferenceSpec(
        section="streaming",
        key="upload_chunks_per_frame",
        env_var="CAVEVIEWER_UPLOAD_CHUNKS_PER_FRAME",
        label="Chunk uploads per frame",
        hint="Max ready chunks uploaded each frame.",
        value_type=PreferenceValueType.INT,
        default="1",
        minimum=1,
        maximum=16,
        units="chunks",
    ),
    PreferenceSpec(
        section="streaming",
        key="upload_groups_per_frame",
        env_var="CAVEVIEWER_UPLOAD_GROUPS_PER_FRAME",
        label="Upload operations per frame",
        hint="Max render-thread upload slices from one ready chunk.",
        value_type=PreferenceValueType.INT,
        default="1",
        minimum=1,
        maximum=64,
        units="operations",
    ),
    PreferenceSpec(
        section="streaming",
        key="upload_time_budget_ms",
        env_var="CAVEVIEWER_UPLOAD_TIME_BUDGET_MS",
        label="Upload budget",
        hint="Target milliseconds spent uploading chunks each frame.",
        value_type=PreferenceValueType.FLOAT,
        default="3.0",
        minimum=0.5,
        maximum=50.0,
        units="ms",
    ),
    PreferenceSpec(
        section="parsing",
        key="chunk_size_meters",
        env_var="CAVEVIEWER_CHUNK_SIZE_METERS",
        label="Import chunk size",
        hint="Unitless chunk edge length for new caches.",
        value_type=PreferenceValueType.FLOAT,
        default="50",
        minimum=0.01,
        maximum=512.0,
    ),
    PreferenceSpec(
        section="parsing",
        key="max_upload_group_mb",
        env_var="CAVEVIEWER_MAX_UPLOAD_GROUP_MB",
        label="Max upload group size",
        hint="Maximum VBO payload size for dense chunk groups, in MB.",
        value_type=PreferenceValueType.FLOAT,
        default="16",
        minimum=1.0,
        maximum=512.0,
        units="MB",
    ),
    PreferenceSpec(
        section="parsing",
        key="obj_scan_throttle_ms",
        env_var="CAVEVIEWER_OBJ_SCAN_THROTTLE_MS",
        label=".obj scan throttle",
        hint="Milliseconds paused while scanning .obj files.",
        value_type=PreferenceValueType.FLOAT,
        default=_scan_throttle_default,
        minimum=0.0,
        maximum=50.0,
        units="ms",
    ),
    PreferenceSpec(
        section="parsing",
        key="obj_import_batch_thousands",
        env_var="CAVEVIEWER_OBJ_IMPORT_BATCH_FACES",
        label="Faces per .obj batch",
        hint="Thousands of triangulated faces per batch.",
        value_type=PreferenceValueType.INT,
        default="200",
        minimum=1,
        maximum=2000,
        units="thousand faces",
        env_to_preference=_faces_env_to_thousands,
        preference_to_env=_thousands_to_faces_env,
    ),
    PreferenceSpec(
        section="parsing",
        key="chunk_build_workers",
        env_var="CAVEVIEWER_CHUNK_BUILD_WORKERS",
        label="Cache-building worker limit",
        hint="Max cache-building worker threads.",
        value_type=PreferenceValueType.INT,
        default="1",
        minimum=1,
        maximum=32,
        units="workers",
    ),
    PreferenceSpec(
        section="parsing",
        key="chunk_build_reserved_cpus",
        env_var="CAVEVIEWER_CHUNK_BUILD_RESERVED_CPUS",
        label="Cache-build CPUs to keep free",
        hint="Logical CPUs reserved from cache build.",
        value_type=PreferenceValueType.INT,
        default="2",
        minimum=2,
        maximum=32,
        units="logical CPUs",
    ),
    PreferenceSpec(
        section="storage",
        key="recording_dir",
        env_var="CAVEVIEWER_RECORDING_DIR",
        label="Recordings folder",
        hint="Where saved recordings are stored.",
        value_type=PreferenceValueType.PATH_CREATE,
        default=_recording_directory_default,
    ),
    PreferenceSpec(
        section="storage",
        key="map_library_dir",
        env_var="CAVEVIEWER_MAP_LIBRARY_DIR",
        label="Downloaded maps folder",
        hint="Where CaveViewer stores downloaded Map Library maps.",
        value_type=PreferenceValueType.PATH_CREATE,
        default=_map_library_directory_default,
    ),
)


def default_preferences() -> dict[str, str]:
    return {field.key: "" for field in PREFERENCE_FIELDS}


def default_recording_dir() -> str:
    configured = os.getenv("CAVEVIEWER_RECORDING_DIR", "").strip()
    return configured or _recording_directory_default()


def default_map_library_dir() -> str:
    configured = os.getenv("CAVEVIEWER_MAP_LIBRARY_DIR", "").strip()
    return configured or _map_library_directory_default()


def normalize_preferences(values: Mapping | None) -> dict[str, str]:
    normalized = default_preferences()
    if not isinstance(values, Mapping):
        return normalized
    for field in PREFERENCE_FIELDS:
        raw = values.get(field.key, "")
        normalized[field.key] = str(raw).strip() if raw is not None else ""
    return normalized


def _format_preference_range(field: PreferenceSpec) -> str:
    suffix = f" {field.units}" if field.units else ""
    if field.minimum is not None and field.maximum is not None:
        return (
            f"at least {field.minimum:g} and no more than "
            f"{field.maximum:g}{suffix}"
        )
    if field.minimum is not None:
        return f"at least {field.minimum:g}{suffix}"
    if field.maximum is not None:
        return f"no more than {field.maximum:g}{suffix}"
    return "valid"


def preference_range_text(
    field: PreferenceSpec, *, include_units: bool = True
) -> str | None:
    if field.value_type not in {PreferenceValueType.INT, PreferenceValueType.FLOAT}:
        return None
    if field.minimum is None or field.maximum is None:
        return None
    suffix = ""
    if include_units:
        suffix = "%" if field.units == "percent" else (
            f" {field.units}" if field.units else ""
        )
    return f"{field.minimum:g}-{field.maximum:g}{suffix}"


def preference_placeholder_text(field: PreferenceSpec) -> str | None:
    return preference_range_text(field, include_units=False)


def _directory_target_is_writable(path: str) -> bool:
    current = path
    while current and not os.path.exists(current):
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    return bool(current and os.path.isdir(current) and os.access(current, os.W_OK))


def validate_preference(
    field: PreferenceSpec, raw_value: object
) -> PreferenceFieldValidationResult:
    text = str(raw_value).strip() if raw_value is not None else ""
    if not text:
        if field.optional:
            return PreferenceFieldValidationResult(True, None, "")
        return PreferenceFieldValidationResult(False, f"{field.label} is required.", text)

    if (
        field.minimum is not None
        and field.minimum >= 0
        and text.startswith("-")
    ):
        return PreferenceFieldValidationResult(
            False, f"{field.label} cannot be negative.", text
        )

    if field.value_type is PreferenceValueType.INT:
        try:
            value = int(text)
        except ValueError:
            return PreferenceFieldValidationResult(
                False, f"{field.label} must be a whole number.", text
            )
        if field.minimum is not None and value < field.minimum:
            return PreferenceFieldValidationResult(
                False,
                f"{field.label} must be {_format_preference_range(field)}.",
                text,
            )
        if field.maximum is not None and value > field.maximum:
            return PreferenceFieldValidationResult(
                False,
                f"{field.label} must be {_format_preference_range(field)}.",
                text,
            )
        return PreferenceFieldValidationResult(True, None, str(value))

    if field.value_type is PreferenceValueType.FLOAT:
        try:
            value = float(text)
        except ValueError:
            return PreferenceFieldValidationResult(
                False, f"{field.label} must be a number.", text
            )
        if not math.isfinite(value):
            return PreferenceFieldValidationResult(
                False, f"{field.label} must be a finite number.", text
            )
        if field.minimum is not None and value < field.minimum:
            return PreferenceFieldValidationResult(
                False,
                f"{field.label} must be {_format_preference_range(field)}.",
                text,
            )
        if field.maximum is not None and value > field.maximum:
            return PreferenceFieldValidationResult(
                False,
                f"{field.label} must be {_format_preference_range(field)}.",
                text,
            )
        return PreferenceFieldValidationResult(True, None, f"{value:g}")

    if field.value_type is PreferenceValueType.PATH:
        path = os.path.abspath(os.path.expanduser(text))
        if not os.path.isdir(path):
            return PreferenceFieldValidationResult(
                False, f"{field.label} must be an existing folder.", text
            )
        if not os.access(path, os.W_OK):
            return PreferenceFieldValidationResult(
                False, f"{field.label} must be writable.", text
            )
        return PreferenceFieldValidationResult(True, None, path)

    if field.value_type is PreferenceValueType.PATH_CREATE:
        path = os.path.abspath(os.path.expanduser(text))
        if os.path.exists(path):
            if not os.path.isdir(path):
                return PreferenceFieldValidationResult(
                    False, f"{field.label} must be a folder.", text
                )
            if not os.access(path, os.W_OK):
                return PreferenceFieldValidationResult(
                    False, f"{field.label} must be writable.", text
                )
        elif not _directory_target_is_writable(path):
            return PreferenceFieldValidationResult(
                False,
                f"{field.label} must be inside a writable folder.",
                text,
            )
        return PreferenceFieldValidationResult(True, None, path)

    return PreferenceFieldValidationResult(True, None, text)


def validate_preferences(values: Mapping[str, str]) -> PreferencesValidationResult:
    normalized = normalize_preferences(values)
    for field in PREFERENCE_FIELDS:
        result = validate_preference(field, normalized[field.key])
        normalized[field.key] = result.normalized_value
        if not result.is_valid:
            return PreferencesValidationResult(
                False,
                result.message,
                MappingProxyType(normalized),
                field.key,
                None,
            )
    preferences = Preferences(normalized)
    return PreferencesValidationResult(
        True, None, MappingProxyType(normalized), None, preferences
    )


def _validated_default(field: PreferenceSpec) -> str:
    configured = os.getenv(field.env_var, "").strip()
    if configured:
        try:
            configured_value = field.value_from_env(configured)
        except Exception as exc:
            _LOG.warning(
                "Ignoring invalid %s value %r: %s",
                field.env_var,
                configured,
                exc,
            )
        else:
            configured_result = validate_preference(field, configured_value)
            if configured_result.is_valid:
                return configured_result.normalized_value
            _LOG.warning(
                "Ignoring invalid %s value %r: %s",
                field.env_var,
                configured,
                configured_result.message,
            )

    built_in = field.built_in_default()
    result = validate_preference(field, built_in)
    if not result.is_valid:
        raise RuntimeError(
            f"Invalid built-in default for {field.key}: {result.message}"
        )
    return result.normalized_value


def preference_defaults() -> dict[str, str]:
    return {field.key: _validated_default(field) for field in PREFERENCE_FIELDS}


def resolve_preferences(values: Mapping | None = None) -> Preferences:
    raw_values = normalize_preferences(values)
    defaults = preference_defaults()
    resolved: dict[str, str] = {}
    for field in PREFERENCE_FIELDS:
        candidate = raw_values[field.key] or defaults[field.key]
        result = validate_preference(field, candidate)
        if result.is_valid:
            resolved[field.key] = result.normalized_value
            continue
        _LOG.warning(
            "Ignoring invalid saved %s value %r: %s",
            field.key,
            candidate,
            result.message,
        )
        resolved[field.key] = defaults[field.key]
    return Preferences(resolved)


def require_validated_preferences(
    values: Mapping[str, str],
) -> Preferences:
    result = validate_preferences(values)
    if not result.is_valid or result.preferences is None:
        raise PreferencesValidationError(result)
    return result.preferences


def preference_env_updates(
    preferences: Preferences,
    fields: Sequence[PreferenceSpec] = PREFERENCE_FIELDS,
) -> dict[str, str]:
    """Return environment assignments for a validated preference snapshot."""
    if not isinstance(preferences, Preferences):
        raise TypeError("preference_env_updates requires a Preferences snapshot")
    return {
        field.env_var: field.value_to_env(preferences[field.key])
        for field in fields
    }
