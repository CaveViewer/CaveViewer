"""Advanced Settings schema, persistence, validation, and environment mapping.

This module deliberately has no Tkinter dependency.  The splash screen owns
presentation; this module owns the values and rules so they can be exercised
with fast, deterministic tests.
"""

from __future__ import annotations

import json
import math
import os
import sys
from pathlib import Path

from core.logging_utils import get_logger
from gui.preferences import migrate_preference_file


_LOG = get_logger("AdvancedSettings")

ADVANCED_SETTING_FIELDS = (
    {
        "section": "streaming",
        "key": "memory_target_percent",
        "env_var": "CAVEVIEWER_MEMORY_UTILIZATION_TARGET",
        "label": "System RAM target (%)",
        "hint": "Limits RAM used by loaded chunks only.",
        "value_type": "float",
        "min": 1.0,
        "max": 80.0,
        "units": "percent",
    },
    {
        "section": "streaming",
        "key": "gpu_memory_target_percent",
        "env_var": "CAVEVIEWER_GPU_MEMORY_UTILIZATION_TARGET",
        "label": "GPU memory target (%)",
        "hint": "GPU memory limit for loaded chunks.",
        "value_type": "float",
        "min": 1.0,
        "max": 80.0,
        "units": "percent",
    },
    {
        "section": "streaming",
        "key": "gpu_memory_gb",
        "env_var": "CAVEVIEWER_GPU_MEMORY_GB",
        "label": "GPU memory override (GB)",
        "hint": "Optional GPU memory override.",
        "value_type": "float",
        "optional": True,
        "min": 0.0,
        "min_exclusive": True,
        "max": 1024.0,
        "units": "GB",
    },
    {
        "section": "streaming",
        "key": "io_workers",
        "env_var": "CAVEVIEWER_IO_WORKERS",
        "label": "Chunk-loading workers",
        "hint": "Threads used while viewing a cave.",
        "value_type": "int",
        "min": 1,
    },
    {
        "section": "streaming",
        "key": "io_reserved_cpus",
        "env_var": "CAVEVIEWER_IO_RESERVED_CPUS",
        "label": "Loading CPUs to keep free",
        "hint": "CPUs reserved during cave viewing.",
        "value_type": "int",
        "min": 0,
    },
    {
        "section": "streaming",
        "key": "upload_chunks_per_frame",
        "env_var": "CAVEVIEWER_UPLOAD_CHUNKS_PER_FRAME",
        "label": "Chunk uploads per frame",
        "hint": "Ready chunks uploaded per frame.",
        "value_type": "int",
        "min": 1,
        "max": 16,
    },
    {
        "section": "streaming",
        "key": "upload_time_budget_ms",
        "env_var": "CAVEVIEWER_UPLOAD_TIME_BUDGET_MS",
        "label": "Upload budget (ms)",
        "hint": "Soft per-frame upload budget.",
        "value_type": "float",
        "min": 0.5,
        "max": 50.0,
        "units": "ms",
    },
    {
        "section": "parsing",
        "key": "chunk_size_meters",
        "env_var": "CAVEVIEWER_CHUNK_SIZE_METERS",
        "label": "Import chunk size (m)",
        "hint": "Chunk size for new caches.",
        "value_type": "float",
        "min": 0.0,
        "min_exclusive": True,
        "max": 512.0,
        "units": "m",
    },
    {
        "section": "parsing",
        "key": "obj_scan_throttle_ms",
        "env_var": "CAVEVIEWER_OBJ_SCAN_THROTTLE_MS",
        "label": "OBJ scan throttle (ms)",
        "hint": "Yield during OBJ scanning.",
        "value_type": "float",
        "min": 0.0,
        "max": 50.0,
        "units": "ms",
    },
    {
        "section": "parsing",
        "key": "chunk_build_workers",
        "env_var": "CAVEVIEWER_CHUNK_BUILD_WORKERS",
        "label": "Cache-building workers",
        "hint": "Threads used to build a new cache.",
        "value_type": "int",
        "min": 1,
    },
    {
        "section": "parsing",
        "key": "chunk_build_reserved_cpus",
        "env_var": "CAVEVIEWER_CHUNK_BUILD_RESERVED_CPUS",
        "label": "Cache-build CPUs to keep free",
        "hint": "CPUs reserved while building a cache.",
        "value_type": "int",
        "min": 0,
    },
    {
        "section": "downloads",
        "key": "recording_dir",
        "env_var": "CAVEVIEWER_RECORDING_DIR",
        "label": "Movie recording directory",
        "hint": "Folder where MP4 flight recordings are saved.",
        "value_type": "path_create",
    },
)

ADVANCED_SETTING_COLUMNS = (
    (("streaming", "Streaming Performance"),),
    (("parsing", "Map Parsing"), ("downloads", "Recordings")),
)


def advanced_settings_file() -> str:
    """Return the migrated preferences path without fixing it at import time."""
    return migrate_preference_file(
        "advanced_settings.json", ".caveviewer_advanced_settings.json"
    )


def default_advanced_settings() -> dict[str, str]:
    return {field["key"]: "" for field in ADVANCED_SETTING_FIELDS}


def _env_setting_or_default(env_var: str, default: str) -> str:
    value = os.getenv(env_var, "").strip()
    return value if value else default


def default_recording_dir() -> str:
    configured = os.getenv("CAVEVIEWER_RECORDING_DIR", "").strip()
    if configured:
        return os.path.abspath(os.path.expanduser(configured))
    return os.path.abspath(os.path.expanduser(os.path.join("~", "Movies", "CaveViewer")))


def advanced_setting_defaults() -> dict[str, str]:
    return {
        "memory_target_percent": _env_setting_or_default(
            "CAVEVIEWER_MEMORY_UTILIZATION_TARGET", "8"
        ),
        "gpu_memory_target_percent": _env_setting_or_default(
            "CAVEVIEWER_GPU_MEMORY_UTILIZATION_TARGET", "70"
        ),
        "gpu_memory_gb": os.getenv("CAVEVIEWER_GPU_MEMORY_GB", "").strip(),
        "io_reserved_cpus": _env_setting_or_default("CAVEVIEWER_IO_RESERVED_CPUS", "3"),
        "io_workers": _env_setting_or_default("CAVEVIEWER_IO_WORKERS", "2"),
        "upload_chunks_per_frame": _env_setting_or_default(
            "CAVEVIEWER_UPLOAD_CHUNKS_PER_FRAME", "1"
        ),
        "upload_time_budget_ms": _env_setting_or_default(
            "CAVEVIEWER_UPLOAD_TIME_BUDGET_MS", "3.0"
        ),
        "chunk_size_meters": _env_setting_or_default(
            "CAVEVIEWER_CHUNK_SIZE_METERS", "8"
        ),
        "obj_scan_throttle_ms": _env_setting_or_default(
            "CAVEVIEWER_OBJ_SCAN_THROTTLE_MS",
            "1" if sys.platform.startswith("win") else "0",
        ),
        "chunk_build_reserved_cpus": _env_setting_or_default(
            "CAVEVIEWER_CHUNK_BUILD_RESERVED_CPUS", "2"
        ),
        "chunk_build_workers": _env_setting_or_default(
            "CAVEVIEWER_CHUNK_BUILD_WORKERS", "1"
        ),
        "recording_dir": default_recording_dir(),
    }


def normalize_advanced_settings(values: dict | None) -> dict[str, str]:
    normalized = default_advanced_settings()
    if not isinstance(values, dict):
        return normalized
    for field in ADVANCED_SETTING_FIELDS:
        raw = values.get(field["key"], "")
        normalized[field["key"]] = str(raw).strip() if raw is not None else ""
    return normalized


def effective_advanced_settings(values: dict | None = None) -> dict[str, str]:
    normalized = normalize_advanced_settings(values)
    defaults = advanced_setting_defaults()
    return {key: normalized.get(key, "") or defaults[key] for key in defaults}


def load_advanced_settings(settings_path: str | os.PathLike[str] | None = None) -> dict[str, str]:
    path = Path(settings_path) if settings_path is not None else Path(advanced_settings_file())
    try:
        with path.open("r", encoding="utf-8") as file_obj:
            return normalize_advanced_settings(json.load(file_obj))
    except Exception:
        return default_advanced_settings()


def save_advanced_settings(
    values: dict[str, str], settings_path: str | os.PathLike[str] | None = None
) -> None:
    path = Path(settings_path) if settings_path is not None else Path(advanced_settings_file())
    try:
        with path.open("w", encoding="utf-8") as file_obj:
            json.dump(normalize_advanced_settings(values), file_obj, indent=2)
    except Exception as exc:
        _LOG.warning("could not save advanced settings (%s)", exc)


def _format_advanced_range(field: dict) -> str:
    minimum = field.get("min")
    maximum = field.get("max")
    units = field.get("units", "")
    suffix = f" {units}" if units else ""
    if minimum is not None and maximum is not None:
        lower = (
            f"greater than {minimum:g}"
            if field.get("min_exclusive")
            else f"at least {minimum:g}"
        )
        return f"{lower} and no more than {maximum:g}{suffix}"
    if minimum is not None:
        return (
            f"greater than {minimum:g}{suffix}"
            if field.get("min_exclusive")
            else f"at least {minimum:g}{suffix}"
        )
    if maximum is not None:
        return f"no more than {maximum:g}{suffix}"
    return "valid"


def _directory_target_is_writable(path: str) -> bool:
    current = path
    while current and not os.path.exists(current):
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    return bool(current and os.path.isdir(current) and os.access(current, os.W_OK))


def validate_advanced_settings(
    values: dict[str, str],
) -> tuple[bool, str | None, dict[str, str], str | None]:
    normalized = normalize_advanced_settings(values)

    for field in ADVANCED_SETTING_FIELDS:
        key = field["key"]
        text = normalized[key]
        if not text:
            if field.get("optional", False):
                continue
            return False, f"{field['label']} is required.", normalized, key

        label = field["label"]
        value_type = field.get("value_type")
        minimum = field.get("min")
        maximum = field.get("max")
        min_exclusive = bool(field.get("min_exclusive"))

        if minimum is not None and minimum >= 0 and text.startswith("-"):
            return False, f"{label} cannot be negative.", normalized, key

        if value_type == "int":
            try:
                value = int(text)
            except ValueError:
                return False, f"{label} must be a whole number.", normalized, key
            if minimum is not None and value < minimum:
                return False, f"{label} must be {_format_advanced_range(field)}.", normalized, key
            if maximum is not None and value > maximum:
                return False, f"{label} must be {_format_advanced_range(field)}.", normalized, key
            normalized[key] = str(value)
            continue

        if value_type == "float":
            try:
                value = float(text)
            except ValueError:
                return False, f"{label} must be a number.", normalized, key
            if not math.isfinite(value):
                return False, f"{label} must be a finite number.", normalized, key
            if minimum is not None:
                below_minimum = value <= minimum if min_exclusive else value < minimum
                if below_minimum:
                    return False, f"{label} must be {_format_advanced_range(field)}.", normalized, key
            if maximum is not None and value > maximum:
                return False, f"{label} must be {_format_advanced_range(field)}.", normalized, key
            normalized[key] = f"{value:g}"
            continue

        if value_type == "path":
            path = os.path.abspath(os.path.expanduser(text))
            if not os.path.isdir(path):
                return False, f"{label} must be an existing folder.", normalized, key
            if not os.access(path, os.W_OK):
                return False, f"{label} must be writable.", normalized, key
            normalized[key] = path
            continue

        if value_type == "path_create":
            path = os.path.abspath(os.path.expanduser(text))
            if os.path.exists(path):
                if not os.path.isdir(path):
                    return False, f"{label} must be a folder.", normalized, key
                if not os.access(path, os.W_OK):
                    return False, f"{label} must be writable.", normalized, key
            elif not _directory_target_is_writable(path):
                return False, f"{label} must be inside a writable folder.", normalized, key
            normalized[key] = path

    return True, None, normalized, None


def apply_advanced_settings_to_env(values: dict[str, str]) -> None:
    normalized = effective_advanced_settings(values)
    for field in ADVANCED_SETTING_FIELDS:
        os.environ[field["env_var"]] = normalized[field["key"]]
