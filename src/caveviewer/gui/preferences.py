"""Typed schema, validation, persistence, and runtime mapping for settings."""

from __future__ import annotations

import json
import math
import os
import sys
import tempfile
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Callable

from caveviewer.core.logging_utils import get_logger
from caveviewer.gui.preference_paths import migrate_preference_file


_LOG = get_logger("Preferences")


class ValueType(str, Enum):
    INT = "int"
    FLOAT = "float"
    PATH = "path"
    PATH_CREATE = "path_create"


DefaultProvider = str | Callable[[], str]
SettingEnvConverter = Callable[[str], str]


@dataclass(frozen=True)
class SettingSpec:
    section: str
    key: str
    env_var: str
    label: str
    hint: str
    value_type: ValueType
    default: DefaultProvider
    optional: bool = False
    minimum: float | int | None = None
    maximum: float | int | None = None
    units: str = ""
    env_to_setting: SettingEnvConverter | None = None
    setting_to_env: SettingEnvConverter | None = None

    def built_in_default(self) -> str:
        value = self.default() if callable(self.default) else self.default
        return str(value).strip()

    def value_from_env(self, raw_value: str) -> str:
        value = str(raw_value).strip()
        if self.env_to_setting is None:
            return value
        return self.env_to_setting(value)

    def value_to_env(self, setting_value: str) -> str:
        value = str(setting_value).strip()
        if self.setting_to_env is None:
            return value
        return self.setting_to_env(value)


@dataclass(frozen=True)
class FieldValidationResult:
    is_valid: bool
    message: str | None
    normalized_value: str


@dataclass(frozen=True, eq=False)
class AdvancedSettings(Mapping[str, str]):
    """Immutable validated settings snapshot."""

    __hash__ = None
    _values: Mapping[str, str]

    def __post_init__(self) -> None:
        raw_values = dict(self._values)
        expected_keys = {field.key for field in ADVANCED_SETTING_FIELDS}
        if set(raw_values) != expected_keys:
            raise ValueError(
                "AdvancedSettings requires exactly the declared schema keys"
            )

        normalized: dict[str, str] = {}
        for field in ADVANCED_SETTING_FIELDS:
            result = validate_advanced_setting(field, raw_values[field.key])
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
class ValidationResult:
    is_valid: bool
    message: str | None
    normalized_values: Mapping[str, str]
    error_key: str | None
    settings: AdvancedSettings | None


class AdvancedSettingsValidationError(ValueError):
    def __init__(self, result: ValidationResult) -> None:
        self.result = result
        super().__init__(result.message or "Invalid advanced settings.")


class AdvancedSettingsSaveError(OSError):
    pass


def _recording_directory_default() -> str:
    return os.path.abspath(
        os.path.expanduser(os.path.join("~", "Movies", "CaveViewer"))
    )


def _scan_throttle_default() -> str:
    return "1" if sys.platform.startswith("win") else "0"


def _faces_env_to_thousands(raw_value: str) -> str:
    face_count = int(str(raw_value).strip())
    if not 1_000 <= face_count <= 2_000_000:
        raise ValueError("face count must be between 1,000 and 2,000,000")
    return str(max(1, round(face_count / 1000)))


def _thousands_to_faces_env(raw_value: str) -> str:
    return str(int(str(raw_value).strip()) * 1000)


ADVANCED_SETTING_FIELDS = (
    SettingSpec(
        section="streaming",
        key="memory_target_percent",
        env_var="CAVEVIEWER_MEMORY_UTILIZATION_TARGET",
        label="System RAM target",
        hint="Target percent of available RAM for loaded chunks.",
        value_type=ValueType.FLOAT,
        default="8",
        minimum=1.0,
        maximum=80.0,
        units="percent",
    ),
    SettingSpec(
        section="streaming",
        key="gpu_memory_target_percent",
        env_var="CAVEVIEWER_GPU_MEMORY_UTILIZATION_TARGET",
        label="GPU memory target",
        hint="Target percent of GPU memory for texture and geometry residency.",
        value_type=ValueType.FLOAT,
        default="70",
        minimum=1.0,
        maximum=80.0,
        units="percent",
    ),
    SettingSpec(
        section="streaming",
        key="gpu_memory_gb",
        env_var="CAVEVIEWER_GPU_MEMORY_GB",
        label="GPU memory override",
        hint="Manual GPU memory ceiling in GB.",
        value_type=ValueType.FLOAT,
        default="",
        optional=True,
        minimum=0.5,
        maximum=50.0,
        units="GB",
    ),
    SettingSpec(
        section="streaming",
        key="io_workers",
        env_var="CAVEVIEWER_IO_WORKERS",
        label="Loading worker limit",
        hint="Max chunk-loading worker threads.",
        value_type=ValueType.INT,
        default="2",
        minimum=1,
        maximum=32,
        units="workers",
    ),
    SettingSpec(
        section="streaming",
        key="io_reserved_cpus",
        env_var="CAVEVIEWER_IO_RESERVED_CPUS",
        label="Loading CPUs to keep free",
        hint="Logical CPUs reserved from loading.",
        value_type=ValueType.INT,
        default="3",
        minimum=2,
        maximum=32,
        units="logical CPUs",
    ),
    SettingSpec(
        section="streaming",
        key="upload_chunks_per_frame",
        env_var="CAVEVIEWER_UPLOAD_CHUNKS_PER_FRAME",
        label="Chunk uploads per frame",
        hint="Max ready chunks uploaded each frame.",
        value_type=ValueType.INT,
        default="1",
        minimum=1,
        maximum=16,
        units="chunks",
    ),
    SettingSpec(
        section="streaming",
        key="upload_groups_per_frame",
        env_var="CAVEVIEWER_UPLOAD_GROUPS_PER_FRAME",
        label="Upload groups per frame",
        hint="Max material groups uploaded from one ready chunk",
        value_type=ValueType.INT,
        default="1",
        minimum=1,
        maximum=64,
        units="groups",
    ),
    SettingSpec(
        section="streaming",
        key="upload_time_budget_ms",
        env_var="CAVEVIEWER_UPLOAD_TIME_BUDGET_MS",
        label="Upload budget",
        hint="Target milliseconds spent uploading chunks each frame.",
        value_type=ValueType.FLOAT,
        default="3.0",
        minimum=0.5,
        maximum=50.0,
        units="ms",
    ),
    SettingSpec(
        section="parsing",
        key="chunk_size_meters",
        env_var="CAVEVIEWER_CHUNK_SIZE_METERS",
        label="Import chunk size",
        hint="Unitless chunk edge length for new caches.",
        value_type=ValueType.FLOAT,
        default="50",
        minimum=0.01,
        maximum=512.0,
    ),
    SettingSpec(
        section="parsing",
        key="max_upload_group_mb",
        env_var="CAVEVIEWER_MAX_UPLOAD_GROUP_MB",
        label="Max upload group size",
        hint="Maximum VBO payload size for dense chunk groups, in MB.",
        value_type=ValueType.FLOAT,
        default="16",
        minimum=1.0,
        maximum=512.0,
        units="MB",
    ),
    SettingSpec(
        section="parsing",
        key="obj_scan_throttle_ms",
        env_var="CAVEVIEWER_OBJ_SCAN_THROTTLE_MS",
        label=".obj scan throttle",
        hint="Milliseconds paused while scanning .obj files.",
        value_type=ValueType.FLOAT,
        default=_scan_throttle_default,
        minimum=0.0,
        maximum=50.0,
        units="ms",
    ),
    SettingSpec(
        section="parsing",
        key="obj_import_batch_thousands",
        env_var="CAVEVIEWER_OBJ_IMPORT_BATCH_FACES",
        label="Faces per .obj batch",
        hint="Thousands of triangulated faces per batch.",
        value_type=ValueType.INT,
        default="200",
        minimum=1,
        maximum=2000,
        units="thousand faces",
        env_to_setting=_faces_env_to_thousands,
        setting_to_env=_thousands_to_faces_env,
    ),
    SettingSpec(
        section="parsing",
        key="chunk_build_workers",
        env_var="CAVEVIEWER_CHUNK_BUILD_WORKERS",
        label="Cache-building worker limit",
        hint="Max cache-building worker threads.",
        value_type=ValueType.INT,
        default="1",
        minimum=1,
        maximum=32,
        units="workers",
    ),
    SettingSpec(
        section="parsing",
        key="chunk_build_reserved_cpus",
        env_var="CAVEVIEWER_CHUNK_BUILD_RESERVED_CPUS",
        label="Cache-build CPUs to keep free",
        hint="Logical CPUs reserved from cache build.",
        value_type=ValueType.INT,
        default="2",
        minimum=2,
        maximum=32,
        units="logical CPUs",
    ),
    SettingSpec(
        section="storage",
        key="recording_dir",
        env_var="CAVEVIEWER_RECORDING_DIR",
        label="Recordings folder",
        hint="Where saved recordings are stored.",
        value_type=ValueType.PATH_CREATE,
        default=_recording_directory_default,
    ),
)

def advanced_settings_file() -> str:
    return migrate_preference_file(
        "advanced_settings.json", ".caveviewer_advanced_settings.json"
    )


def default_advanced_settings() -> dict[str, str]:
    return {field.key: "" for field in ADVANCED_SETTING_FIELDS}


def default_recording_dir() -> str:
    configured = os.getenv("CAVEVIEWER_RECORDING_DIR", "").strip()
    return configured or _recording_directory_default()


def normalize_advanced_settings(values: Mapping | None) -> dict[str, str]:
    normalized = default_advanced_settings()
    if not isinstance(values, Mapping):
        return normalized
    for field in ADVANCED_SETTING_FIELDS:
        raw = values.get(field.key, "")
        normalized[field.key] = str(raw).strip() if raw is not None else ""
    return normalized


def _format_advanced_range(field: SettingSpec) -> str:
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


def advanced_setting_range_text(
    field: SettingSpec, *, include_units: bool = True
) -> str | None:
    if field.value_type not in {ValueType.INT, ValueType.FLOAT}:
        return None
    if field.minimum is None or field.maximum is None:
        return None
    suffix = ""
    if include_units:
        suffix = "%" if field.units == "percent" else (
            f" {field.units}" if field.units else ""
        )
    return f"{field.minimum:g}-{field.maximum:g}{suffix}"


def advanced_setting_placeholder_text(field: SettingSpec) -> str | None:
    return advanced_setting_range_text(field, include_units=False)


def _directory_target_is_writable(path: str) -> bool:
    current = path
    while current and not os.path.exists(current):
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    return bool(current and os.path.isdir(current) and os.access(current, os.W_OK))


def validate_advanced_setting(
    field: SettingSpec, raw_value: object
) -> FieldValidationResult:
    text = str(raw_value).strip() if raw_value is not None else ""
    if not text:
        if field.optional:
            return FieldValidationResult(True, None, "")
        return FieldValidationResult(False, f"{field.label} is required.", text)

    if (
        field.minimum is not None
        and field.minimum >= 0
        and text.startswith("-")
    ):
        return FieldValidationResult(
            False, f"{field.label} cannot be negative.", text
        )

    if field.value_type is ValueType.INT:
        try:
            value = int(text)
        except ValueError:
            return FieldValidationResult(
                False, f"{field.label} must be a whole number.", text
            )
        if field.minimum is not None and value < field.minimum:
            return FieldValidationResult(
                False,
                f"{field.label} must be {_format_advanced_range(field)}.",
                text,
            )
        if field.maximum is not None and value > field.maximum:
            return FieldValidationResult(
                False,
                f"{field.label} must be {_format_advanced_range(field)}.",
                text,
            )
        return FieldValidationResult(True, None, str(value))

    if field.value_type is ValueType.FLOAT:
        try:
            value = float(text)
        except ValueError:
            return FieldValidationResult(
                False, f"{field.label} must be a number.", text
            )
        if not math.isfinite(value):
            return FieldValidationResult(
                False, f"{field.label} must be a finite number.", text
            )
        if field.minimum is not None and value < field.minimum:
            return FieldValidationResult(
                False,
                f"{field.label} must be {_format_advanced_range(field)}.",
                text,
            )
        if field.maximum is not None and value > field.maximum:
            return FieldValidationResult(
                False,
                f"{field.label} must be {_format_advanced_range(field)}.",
                text,
            )
        return FieldValidationResult(True, None, f"{value:g}")

    if field.value_type is ValueType.PATH:
        path = os.path.abspath(os.path.expanduser(text))
        if not os.path.isdir(path):
            return FieldValidationResult(
                False, f"{field.label} must be an existing folder.", text
            )
        if not os.access(path, os.W_OK):
            return FieldValidationResult(
                False, f"{field.label} must be writable.", text
            )
        return FieldValidationResult(True, None, path)

    if field.value_type is ValueType.PATH_CREATE:
        path = os.path.abspath(os.path.expanduser(text))
        if os.path.exists(path):
            if not os.path.isdir(path):
                return FieldValidationResult(
                    False, f"{field.label} must be a folder.", text
                )
            if not os.access(path, os.W_OK):
                return FieldValidationResult(
                    False, f"{field.label} must be writable.", text
                )
        elif not _directory_target_is_writable(path):
            return FieldValidationResult(
                False,
                f"{field.label} must be inside a writable folder.",
                text,
            )
        return FieldValidationResult(True, None, path)

    return FieldValidationResult(True, None, text)


def validate_advanced_settings(values: Mapping[str, str]) -> ValidationResult:
    normalized = normalize_advanced_settings(values)
    for field in ADVANCED_SETTING_FIELDS:
        result = validate_advanced_setting(field, normalized[field.key])
        normalized[field.key] = result.normalized_value
        if not result.is_valid:
            return ValidationResult(
                False,
                result.message,
                MappingProxyType(normalized),
                field.key,
                None,
            )
    settings = AdvancedSettings(normalized)
    return ValidationResult(
        True, None, MappingProxyType(normalized), None, settings
    )


def _validated_default(field: SettingSpec) -> str:
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
            configured_result = validate_advanced_setting(field, configured_value)
            if configured_result.is_valid:
                return configured_result.normalized_value
            _LOG.warning(
                "Ignoring invalid %s value %r: %s",
                field.env_var,
                configured,
                configured_result.message,
            )

    built_in = field.built_in_default()
    result = validate_advanced_setting(field, built_in)
    if not result.is_valid:
        raise RuntimeError(
            f"Invalid built-in default for {field.key}: {result.message}"
        )
    return result.normalized_value


def advanced_setting_defaults() -> dict[str, str]:
    return {field.key: _validated_default(field) for field in ADVANCED_SETTING_FIELDS}


def resolve_advanced_settings(values: Mapping | None = None) -> AdvancedSettings:
    raw_values = normalize_advanced_settings(values)
    defaults = advanced_setting_defaults()
    resolved: dict[str, str] = {}
    for field in ADVANCED_SETTING_FIELDS:
        candidate = raw_values[field.key] or defaults[field.key]
        result = validate_advanced_setting(field, candidate)
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
    return AdvancedSettings(resolved)


def effective_advanced_settings(values: Mapping | None = None) -> AdvancedSettings:
    """Compatibility name for resolving a validated settings snapshot."""
    return resolve_advanced_settings(values)


def require_validated_advanced_settings(
    values: Mapping[str, str],
) -> AdvancedSettings:
    result = validate_advanced_settings(values)
    if not result.is_valid or result.settings is None:
        raise AdvancedSettingsValidationError(result)
    return result.settings


def load_advanced_settings(
    settings_path: str | os.PathLike[str] | None = None,
) -> AdvancedSettings:
    path = (
        Path(settings_path)
        if settings_path is not None
        else Path(advanced_settings_file())
    )
    try:
        with path.open("r", encoding="utf-8") as file_obj:
            payload = json.load(file_obj)
    except Exception as exc:
        if not isinstance(exc, FileNotFoundError):
            _LOG.warning("Could not load advanced settings from %s: %s", path, exc)
        payload = None
    return resolve_advanced_settings(payload if isinstance(payload, Mapping) else None)


def save_advanced_settings(
    settings: AdvancedSettings,
    settings_path: str | os.PathLike[str] | None = None,
) -> None:
    if not isinstance(settings, AdvancedSettings):
        raise TypeError("save_advanced_settings requires an AdvancedSettings snapshot")

    path = (
        Path(settings_path)
        if settings_path is not None
        else Path(advanced_settings_file())
    )
    temp_path: Path | None = None
    try:
        file_descriptor, temp_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
        )
        temp_path = Path(temp_name)
        with os.fdopen(file_descriptor, "w", encoding="utf-8") as file_obj:
            json.dump(settings.as_dict(), file_obj, indent=2)
            file_obj.flush()
            os.fsync(file_obj.fileno())
        os.replace(temp_path, path)
        temp_path = None
    except Exception as exc:
        if temp_path is not None:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass
        _LOG.warning("Could not save advanced settings to %s: %s", path, exc)
        raise AdvancedSettingsSaveError(
            f"Could not save preferences to {path}."
        ) from exc


def apply_advanced_settings_to_env(settings: AdvancedSettings) -> None:
    if not isinstance(settings, AdvancedSettings):
        raise TypeError(
            "apply_advanced_settings_to_env requires an AdvancedSettings snapshot"
        )
    for field in ADVANCED_SETTING_FIELDS:
        os.environ[field.env_var] = field.value_to_env(settings[field.key])
