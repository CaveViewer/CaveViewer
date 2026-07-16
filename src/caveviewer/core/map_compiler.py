"""Command-line map compilation orchestration.

This module sits above :mod:`caveviewer.core.chunker`: it resolves source
models, applies the same import settings exposed by Preferences, selects the
managed cache root, and delegates the actual OBJ/GLB cache build to the
existing app import pipeline.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
import json
import os
from pathlib import Path
from typing import Literal

from caveviewer.core.cache_paths import MANAGED_CACHE_ENV_VAR, MapCacheLocator
from caveviewer.gui.preferences import (
    ADVANCED_SETTING_FIELDS,
    AdvancedSettings,
    require_validated_advanced_settings,
    load_advanced_settings,
    resolve_advanced_settings,
)


PARSING_SETTING_KEYS = frozenset(
    field.key for field in ADVANCED_SETTING_FIELDS if field.section == "parsing"
)


@dataclass(frozen=True)
class CompileOptions:
    """Validated high-level inputs for a non-GUI map compile."""

    source: str
    cache_root: str | None = None
    settings_file: str | None = None
    parsing_overrides: Mapping[str, str] | None = None
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
    settings = _resolve_settings(options.settings_file, options.parsing_overrides)
    chunk_size = _float_setting(settings, "chunk_size_meters")

    env_updates = {
        field.env_var: field.value_to_env(settings[field.key])
        for field in ADVANCED_SETTING_FIELDS
    }
    if cache_root is not None:
        env_updates[MANAGED_CACHE_ENV_VAR] = cache_root

    with _temporary_environ(env_updates):
        return _compile_with_environment(
            options,
            source_argument=source_argument,
            chunk_size=chunk_size,
        )


def _compile_with_environment(
    options: CompileOptions,
    *,
    source_argument: str,
    chunk_size: float,
) -> CompileResult:
    from caveviewer import app
    from caveviewer.core import chunker
    from caveviewer.core.cache_paths import map_cache_build_dir

    selected_path = os.path.abspath(os.path.expanduser(source_argument))
    selected_is_file = os.path.isfile(selected_path)
    textures_dir = os.path.dirname(selected_path) if selected_is_file else selected_path

    try:
        model_descriptor = app.find_model_file(selected_path)
    except FileNotFoundError as exc:
        raise MapCompileConfigurationError(str(exc)) from exc

    source_path = str(model_descriptor.get("obj_path") or model_descriptor.get("glb_path"))
    source_format = str(model_descriptor.get("format") or "")
    cache_dir = os.path.abspath(map_cache_build_dir(source_path))
    try:
        effective_cache_root = str(MapCacheLocator().managed_root)
    except Exception as exc:
        raise MapCompileConfigurationError(str(exc)) from exc

    manifest = chunker.load_manifest(cache_dir)
    cached_chunk_size = chunker.manifest_chunk_size(manifest)
    cache_valid = chunker.cache_is_valid(source_path)
    chunk_size_mismatch = (
        cache_valid
        and cached_chunk_size is not None
        and abs(cached_chunk_size - chunk_size) > 1e-6
    )
    missing_manifest_chunk_size = cache_valid and cached_chunk_size is None
    rebuild_for_chunk_size = chunk_size_mismatch or missing_manifest_chunk_size

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

    if cache_valid and not options.force_rebuild and not rebuild_for_chunk_size:
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

    built_cache_dir = app.import_and_cache_any(
        model_descriptor,
        textures_dir,
        force_rebuild=bool(options.force_rebuild or rebuild_for_chunk_size),
        console_progress=not options.json_output,
        chunk_size=chunk_size,
    )
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
        rebuilt_for_chunk_size=rebuild_for_chunk_size,
    )


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
    rebuilt_for_chunk_size: bool,
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
        rebuilt_for_chunk_size=rebuilt_for_chunk_size,
        force_rebuild=bool(options.force_rebuild),
        dry_run=bool(options.dry_run),
    )


def _resolve_settings(
    settings_file: str | None,
    parsing_overrides: Mapping[str, str] | None,
) -> AdvancedSettings:
    settings = (
        load_advanced_settings()
        if settings_file is None
        else _load_explicit_settings(settings_file)
    )
    values = settings.as_dict()
    for key, value in dict(parsing_overrides or {}).items():
        if key not in PARSING_SETTING_KEYS:
            raise MapCompileConfigurationError(
                f"Unsupported import setting override: {key}"
            )
        values[key] = str(value)

    try:
        return require_validated_advanced_settings(values)
    except Exception as exc:
        raise MapCompileConfigurationError(str(exc)) from exc


def _load_explicit_settings(settings_file: str) -> AdvancedSettings:
    path = Path(settings_file).expanduser()
    if not path.exists():
        raise MapCompileConfigurationError(
            f"--settings-file does not exist: {settings_file}"
        )
    try:
        with path.open("r", encoding="utf-8") as file_obj:
            payload = json.load(file_obj)
    except Exception as exc:
        raise MapCompileConfigurationError(
            f"Could not load --settings-file {path}: {exc}"
        ) from exc
    if not isinstance(payload, Mapping):
        raise MapCompileConfigurationError(
            f"--settings-file must contain a JSON object: {path}"
        )
    return resolve_advanced_settings(payload)


def _float_setting(settings: AdvancedSettings, key: str) -> float:
    try:
        return float(settings[key])
    except Exception as exc:
        raise MapCompileConfigurationError(f"Invalid numeric setting: {key}") from exc


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


@contextmanager
def _temporary_environ(updates: Mapping[str, str]) -> Iterator[None]:
    previous: dict[str, str | None] = {}
    for key, value in updates.items():
        previous[key] = os.environ.get(key)
        os.environ[key] = str(value)
    try:
        yield
    finally:
        for key, old_value in previous.items():
            if old_value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old_value
