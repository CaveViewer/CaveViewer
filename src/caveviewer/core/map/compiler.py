"""Command-line map compilation orchestration.

This module sits above :mod:`caveviewer.core.chunking.builder`: it resolves source
models, applies the same parsing preferences exposed by Preferences, selects the
managed cache root, and delegates the actual OBJ/GLB cache build to the
core import pipeline.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
import os
import time
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from caveviewer.core.preferences.schema import (
    PREFERENCE_FIELDS,
    validate_preference,
)
from caveviewer.core.diagnostics.logging import (
    finish_console_progress_line,
    set_console_progress,
)
from caveviewer.core.json_io import load_bounded_json
from caveviewer.core.map.cache_paths import MANAGED_CACHE_ENV_VAR, MapCacheLocator
from caveviewer.core.map.importer import import_and_cache_any
from caveviewer.core.map.source_model import find_model_file


if TYPE_CHECKING:
    from caveviewer.core.map.chunk_size_advisor import ChunkSizeRecommendation


PARSING_PREFERENCE_FIELDS = tuple(
    field for field in PREFERENCE_FIELDS if field.section == "parsing"
)
PARSING_PREFERENCE_KEYS = frozenset(field.key for field in PARSING_PREFERENCE_FIELDS)
PREFERENCE_KEYS = frozenset(field.key for field in PREFERENCE_FIELDS)
DEFAULT_OBJ_BUCKET_WORKERS = 2
MIN_OBJ_BUCKET_WORKERS = 1
MAX_OBJ_BUCKET_WORKERS = 32
DEFAULT_ANALYZE_WORKERS = 2
MIN_ANALYZE_WORKERS = 1
MAX_ANALYZE_WORKERS = 32
MAX_SETTINGS_FILE_BYTES = 1 * 1024 * 1024
ProgressCallback = Callable[[str, float], None]


@dataclass(frozen=True)
class CompileOptions:
    """Validated high-level inputs for a non-GUI map compile."""

    source: str
    cache_root: str | None = None
    settings_file: str | None = None
    parsing_overrides: Mapping[str, str] | None = None
    obj_bucket_workers: str | None = None
    analyze_workers: str | None = None
    force_rebuild: bool = False
    dry_run: bool = False
    json_output: bool = False


@dataclass(frozen=True)
class CompileResult:
    """Summary of a planned, skipped, or completed compile."""

    status: Literal["planned", "skipped", "built"]
    source_argument: str
    source_path: str
    source_format: str
    textures_dir: str
    cache_root: str
    cache_dir: str
    chunk_size: float
    chunk_count: int | None = None
    triangle_count: int | None = None
    elapsed_seconds: float | None = None
    rebuilt_for_chunk_size: bool = False
    force_rebuild: bool = False
    dry_run: bool = False

    def as_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "source": self.source_path,
            "source_argument": self.source_argument,
            "format": self.source_format,
            "textures_dir": self.textures_dir,
            "cache_root": self.cache_root,
            "cache_dir": self.cache_dir,
            "chunk_size": self.chunk_size,
            "chunk_count": self.chunk_count,
            "triangle_count": self.triangle_count,
            "elapsed_seconds": self.elapsed_seconds,
            "rebuilt_for_chunk_size": self.rebuilt_for_chunk_size,
            "force_rebuild": self.force_rebuild,
            "dry_run": self.dry_run,
        }


class MapCompileError(RuntimeError):
    """Base class for expected map compiler failures."""


class MapCompileConfigurationError(MapCompileError):
    """Raised when CLI/settings/cache-root inputs are invalid."""


def compile_map(options: CompileOptions) -> CompileResult:
    """Compile a map cache or report that the existing cache is reusable."""
    if not isinstance(options, CompileOptions):
        raise TypeError("compile_map requires CompileOptions")

    source_argument = _require_non_empty_path(options.source, "--source")
    cache_root = _normalize_cache_root(options.cache_root)
    preferences = _resolve_preferences(options.settings_file, options.parsing_overrides)
    chunk_size = _float_preference(preferences, "chunk_size_meters")
    max_upload_group_mb = _float_preference(preferences, "max_upload_group_mb")
    obj_bucket_workers = _resolve_obj_bucket_workers(options.obj_bucket_workers)
    face_batch_size = _obj_face_batch_size(preferences)

    return _compile_with_configuration(
        options,
        source_argument=source_argument,
        cache_root=cache_root,
        chunk_size=chunk_size,
        max_upload_group_mb=max_upload_group_mb,
        obj_import_batch_faces=face_batch_size,
        obj_bucket_workers=obj_bucket_workers,
    )


def analyze_chunk_sizes(
    options: CompileOptions,
    *,
    progress_cb: ProgressCallback | None = None,
) -> "ChunkSizeRecommendation":
    """Analyze source geometry and recommend a chunk size without building."""
    if not isinstance(options, CompileOptions):
        raise TypeError("analyze_chunk_sizes requires CompileOptions")

    source_argument = _require_non_empty_path(options.source, "--source")
    cache_root = _normalize_cache_root(options.cache_root)
    preferences = _resolve_preferences(options.settings_file, options.parsing_overrides)
    requested_chunk_size = _float_preference(preferences, "chunk_size_meters")
    obj_bucket_workers = _resolve_obj_bucket_workers(options.obj_bucket_workers)
    face_batch_size = _obj_face_batch_size(preferences)
    analyze_workers = _resolve_analyze_workers(options.analyze_workers)

    del obj_bucket_workers
    return _analyze_with_configuration(
        source_argument=source_argument,
        cache_root=cache_root,
        requested_chunk_size=requested_chunk_size,
        face_batch_size=face_batch_size,
        worker_count=analyze_workers,
        progress_cb=progress_cb,
    )


def _compile_with_configuration(
    options: CompileOptions,
    *,
    source_argument: str,
    cache_root: str | None,
    chunk_size: float,
    max_upload_group_mb: float,
    obj_import_batch_faces: int,
    obj_bucket_workers: int,
) -> CompileResult:
    from caveviewer.core.chunking import builder as chunker

    selected_path = os.path.abspath(os.path.expanduser(source_argument))
    selected_is_file = os.path.isfile(selected_path)
    textures_dir = os.path.dirname(selected_path) if selected_is_file else selected_path

    try:
        model_descriptor = find_model_file(selected_path)
    except FileNotFoundError as exc:
        raise MapCompileConfigurationError(str(exc)) from exc

    source_path = str(model_descriptor.get("obj_path") or model_descriptor.get("glb_path"))
    source_format = str(model_descriptor.get("format") or "")
    try:
        locator = _map_cache_locator(cache_root)
        cache_dir = os.path.abspath(str(locator.build_cache_dir(source_path)))
        effective_cache_root = str(locator.managed_root)
    except Exception as exc:
        raise MapCompileConfigurationError(str(exc)) from exc

    manifest = chunker.load_manifest(cache_dir)
    cached_chunk_size = chunker.manifest_chunk_size(manifest)
    cached_max_upload_group_mb = chunker.manifest_max_upload_group_mb(manifest)
    cache_valid = chunker.cache_dir_is_valid(cache_dir, source_path)
    chunk_size_mismatch = (
        cache_valid
        and cached_chunk_size is not None
        and abs(cached_chunk_size - chunk_size) > 1e-6
    )
    missing_manifest_chunk_size = cache_valid and cached_chunk_size is None
    rebuild_for_chunk_size = chunk_size_mismatch or missing_manifest_chunk_size
    max_upload_group_mismatch = (
        cache_valid
        and cached_max_upload_group_mb is not None
        and abs(cached_max_upload_group_mb - max_upload_group_mb) > 1e-6
    )
    rebuild_for_preferences = (
        rebuild_for_chunk_size
        or max_upload_group_mismatch
    )

    if options.dry_run:
        return _result_from_manifest(
            status="planned",
            options=options,
            source_argument=source_argument,
            source_path=source_path,
            source_format=source_format,
            textures_dir=textures_dir,
            cache_root=effective_cache_root,
            cache_dir=cache_dir,
            chunk_size=chunk_size,
            manifest=manifest,
            rebuilt_for_chunk_size=rebuild_for_chunk_size,
        )

    if cache_valid and not options.force_rebuild and not rebuild_for_preferences:
        return _result_from_manifest(
            status="skipped",
            options=options,
            source_argument=source_argument,
            source_path=source_path,
            source_format=source_format,
            textures_dir=textures_dir,
            cache_root=effective_cache_root,
            cache_dir=cache_dir,
            chunk_size=chunk_size,
            manifest=manifest,
            rebuilt_for_chunk_size=False,
        )

    build_started_at = time.perf_counter()
    progress_cb = None if options.json_output else set_console_progress
    try:
        built_cache_dir = import_and_cache_any(
            model_descriptor,
            textures_dir,
            force_rebuild=bool(options.force_rebuild or rebuild_for_preferences),
            progress_cb=progress_cb,
            chunk_size=chunk_size,
            cache_dir=cache_dir,
            max_upload_group_mb=max_upload_group_mb,
            obj_import_batch_faces=obj_import_batch_faces,
            obj_bucket_workers=obj_bucket_workers,
        )
    finally:
        if progress_cb is not None:
            finish_console_progress_line()
    elapsed_seconds = time.perf_counter() - build_started_at
    built_manifest = chunker.load_manifest(built_cache_dir)
    return _result_from_manifest(
        status="built",
        options=options,
        source_argument=source_argument,
        source_path=source_path,
        source_format=source_format,
        textures_dir=textures_dir,
        cache_root=effective_cache_root,
        cache_dir=built_cache_dir,
        chunk_size=chunk_size,
        manifest=built_manifest,
        elapsed_seconds=elapsed_seconds,
        rebuilt_for_chunk_size=rebuild_for_chunk_size,
    )


def _analyze_with_configuration(
    *,
    source_argument: str,
    cache_root: str | None,
    requested_chunk_size: float,
    face_batch_size: int,
    worker_count: int,
    progress_cb: ProgressCallback | None = None,
) -> "ChunkSizeRecommendation":
    from caveviewer.core.map.chunk_size_advisor import (
        DEFAULT_CANDIDATE_SIZES,
        recommend_chunk_size_for_descriptor,
    )

    selected_path = os.path.abspath(os.path.expanduser(source_argument))
    if cache_root is not None:
        # Validate the same cache-root input as compile mode without mutating
        # process-global environment. Analysis does not currently need the path.
        try:
            _map_cache_locator(cache_root).managed_root
        except Exception as exc:
            raise MapCompileConfigurationError(str(exc)) from exc
    if progress_cb is not None:
        progress_cb("locating source", 0.0)
    try:
        model_descriptor = find_model_file(selected_path)
    except FileNotFoundError as exc:
        raise MapCompileConfigurationError(str(exc)) from exc

    candidate_sizes = tuple(sorted({
        *DEFAULT_CANDIDATE_SIZES,
        float(requested_chunk_size),
    }))
    try:
        return recommend_chunk_size_for_descriptor(
            model_descriptor,
            candidate_sizes=candidate_sizes,
            face_batch_size=face_batch_size,
            worker_count=worker_count,
            progress_cb=progress_cb,
        )
    except ValueError as exc:
        raise MapCompileConfigurationError(str(exc)) from exc


def _result_from_manifest(
    *,
    status: Literal["planned", "skipped", "built"],
    options: CompileOptions,
    source_argument: str,
    source_path: str,
    source_format: str,
    textures_dir: str,
    cache_root: str,
    cache_dir: str,
    chunk_size: float,
    manifest: dict | None,
    elapsed_seconds: float | None = None,
    rebuilt_for_chunk_size: bool = False,
) -> CompileResult:
    chunks = manifest.get("chunks") if isinstance(manifest, dict) else None
    chunk_count = len(chunks) if isinstance(chunks, dict) else None
    triangle_count = manifest.get("triangle_count") if isinstance(manifest, dict) else None
    if not isinstance(triangle_count, int):
        triangle_count = None
    return CompileResult(
        status=status,
        source_argument=source_argument,
        source_path=source_path,
        source_format=source_format,
        textures_dir=textures_dir,
        cache_root=cache_root,
        cache_dir=cache_dir,
        chunk_size=chunk_size,
        chunk_count=chunk_count,
        triangle_count=triangle_count,
        elapsed_seconds=elapsed_seconds,
        rebuilt_for_chunk_size=rebuilt_for_chunk_size,
        force_rebuild=bool(options.force_rebuild),
        dry_run=bool(options.dry_run),
    )


def _resolve_preferences(
    settings_file: str | None,
    parsing_overrides: Mapping[str, str] | None,
) -> Mapping[str, str]:
    preferences = (
        _built_in_preferences()
        if settings_file is None
        else _load_explicit_preferences(settings_file)
    )
    values = dict(preferences)
    for key, value in dict(parsing_overrides or {}).items():
        if key not in PARSING_PREFERENCE_KEYS:
            raise MapCompileConfigurationError(
                f"Unsupported parsing preference override: {key}"
            )
        values[key] = str(value)

    return _validate_parsing_preferences(values)


def _load_explicit_preferences(settings_file: str) -> Mapping[str, str]:
    path = Path(settings_file).expanduser()
    if not path.exists():
        raise MapCompileConfigurationError(
            f"--settings-file does not exist: {settings_file}"
        )
    try:
        payload = load_bounded_json(
            path,
            max_bytes=MAX_SETTINGS_FILE_BYTES,
            description="settings file",
        )
    except Exception as exc:
        raise MapCompileConfigurationError(
            f"Could not load --settings-file {path}: {exc}"
        ) from exc
    if not isinstance(payload, Mapping):
        raise MapCompileConfigurationError(
            f"--settings-file must contain a JSON object: {path}"
        )
    values = dict(_built_in_preferences())
    for key, value in payload.items():
        key = str(key)
        if key not in PREFERENCE_KEYS:
            raise MapCompileConfigurationError(
                f"--settings-file contains an unknown preference: {key}"
            )
        if key in PARSING_PREFERENCE_KEYS:
            values[key] = str(value).strip() if value is not None else ""
    return _validate_parsing_preferences(values)


def _built_in_preferences() -> Mapping[str, str]:
    values: dict[str, str] = {}
    for field in PARSING_PREFERENCE_FIELDS:
        result = validate_preference(field, field.built_in_default())
        if not result.is_valid:
            raise MapCompileConfigurationError(
                f"Invalid built-in default for {field.key}: {result.message}"
            )
        values[field.key] = result.normalized_value
    return values


def _validate_parsing_preferences(values: Mapping[str, str]) -> Mapping[str, str]:
    normalized: dict[str, str] = {}
    for field in PARSING_PREFERENCE_FIELDS:
        result = validate_preference(field, values.get(field.key, ""))
        if not result.is_valid:
            raise MapCompileConfigurationError(
                result.message or f"Invalid value for {field.key}"
            )
        normalized[field.key] = result.normalized_value
    return normalized


def _float_preference(preferences: Mapping[str, str], key: str) -> float:
    try:
        return float(preferences[key])
    except Exception as exc:
        raise MapCompileConfigurationError(f"Invalid numeric preference: {key}") from exc


def _obj_face_batch_size(preferences: Mapping[str, str]) -> int:
    try:
        return int(preferences["obj_import_batch_thousands"]) * 1000
    except Exception as exc:
        raise MapCompileConfigurationError(
            "Invalid numeric preference: obj_import_batch_thousands"
        ) from exc


def _resolve_obj_bucket_workers(raw_value: str | None) -> int:
    text = str(raw_value).strip() if raw_value is not None else ""
    if not text:
        return DEFAULT_OBJ_BUCKET_WORKERS
    try:
        value = int(text)
    except ValueError as exc:
        raise MapCompileConfigurationError(
            "--obj-bucket-workers must be a whole number."
        ) from exc
    if value < MIN_OBJ_BUCKET_WORKERS or value > MAX_OBJ_BUCKET_WORKERS:
        raise MapCompileConfigurationError(
            "--obj-bucket-workers must be at least 1 and no more than 32 workers."
        )
    return value


def _resolve_analyze_workers(raw_value: str | None) -> int:
    text = str(raw_value).strip() if raw_value is not None else ""
    if not text:
        return DEFAULT_ANALYZE_WORKERS
    try:
        value = int(text)
    except ValueError as exc:
        raise MapCompileConfigurationError(
            "--analyze-workers must be a whole number."
        ) from exc
    if value < MIN_ANALYZE_WORKERS or value > MAX_ANALYZE_WORKERS:
        raise MapCompileConfigurationError(
            "--analyze-workers must be at least 1 and no more than 32 workers."
        )
    return value


def _require_non_empty_path(value: object, option_name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise MapCompileConfigurationError(f"{option_name} requires a value.")
    return text


def _normalize_cache_root(cache_root: str | None) -> str | None:
    if cache_root is None:
        return None
    raw = str(cache_root).strip()
    if not raw:
        raise MapCompileConfigurationError("--cache-root requires a value.")
    expanded = os.path.expanduser(raw)
    if not os.path.isabs(expanded):
        raise MapCompileConfigurationError(
            f"--cache-root must be an absolute path: {cache_root!r}"
        )
    normalized = os.path.abspath(expanded)
    if os.path.exists(normalized):
        if not os.path.isdir(normalized):
            raise MapCompileConfigurationError(
                f"--cache-root must be a directory: {normalized}"
            )
        if not os.access(normalized, os.W_OK):
            raise MapCompileConfigurationError(
                f"--cache-root must be writable: {normalized}"
            )
        return normalized

    parent = os.path.dirname(normalized)
    while parent and not os.path.exists(parent):
        next_parent = os.path.dirname(parent)
        if next_parent == parent:
            break
        parent = next_parent
    if not parent or not os.path.isdir(parent) or not os.access(parent, os.W_OK):
        raise MapCompileConfigurationError(
            f"--cache-root must be inside a writable folder: {normalized}"
        )
    return normalized


def _map_cache_locator(cache_root: str | None) -> MapCacheLocator:
    if cache_root is None:
        return MapCacheLocator()
    return MapCacheLocator(environ={MANAGED_CACHE_ENV_VAR: cache_root})
