"""Core model import and cache-build orchestration.

The app/GUI layer supplies progress rendering and pause signals; this module
owns the non-UI workflow that turns supported model descriptors into generated
chunk caches.
"""

from __future__ import annotations

import os
from collections.abc import Callable

from caveviewer.core.chunking import builder as chunker
from caveviewer.core.chunking.staging import ResumeCheckpointUnavailableError
from caveviewer.core.diagnostics.logging import get_logger
from caveviewer.core.mesh import glb as glb_parser
from caveviewer.core.mesh import obj as obj_parser
from caveviewer.core.map import source_model
from caveviewer.core.map.cache_paths import map_cache_build_dir
from caveviewer.core.textures.decoding import resolve_texture_path


ProgressCallback = Callable[[str, float], None]
PauseCallback = Callable[[], bool]

_LOG = get_logger("MapImporter")


def _emit_progress(
    progress_cb: ProgressCallback | None,
    stage: str,
    fraction: float,
) -> None:
    if progress_cb is None:
        return
    progress_cb(stage, max(0.0, min(1.0, float(fraction))))


def import_and_cache(
    obj_path: str,
    mtl_path: str,
    force_rebuild: bool = False,
    *,
    progress_cb: ProgressCallback | None = None,
    pause_requested: PauseCallback | None = None,
    chunk_size: float | None = None,
    cache_dir: str | None = None,
    max_upload_group_mb: float | None = None,
    obj_import_batch_faces: int | None = None,
    obj_bucket_workers: int | None = None,
    resume_required: bool = False,
) -> str:
    """Parse and cache one OBJ source, reusing an existing valid cache."""
    target_cache_dir = os.path.abspath(cache_dir or map_cache_build_dir(obj_path))
    if not force_rebuild and cache_dir is not None:
        if chunker.cache_dir_is_valid(target_cache_dir, obj_path):
            _LOG.info(
                "Using an existing chunk cache; remove the reported cache directory "
                "to force a rebuild."
            )
            _LOG.info("Found cache in: %s", target_cache_dir)
            return target_cache_dir
    elif not force_rebuild and chunker.cache_is_valid(obj_path):
        cache_dir = chunker.get_cache_dir(obj_path)
        _LOG.info(
            "Using an existing chunk cache; remove the reported cache directory "
            "to force a rebuild."
        )
        _LOG.info("Found cache in: %s", cache_dir)
        return cache_dir

    materials = obj_parser.parse_mtl(mtl_path)
    texture_assets = file_texture_assets(
        materials, os.path.dirname(os.path.abspath(mtl_path))
    )

    # Reject imports that lack cache-disk headroom before parsing a potentially
    # multi-gigabyte source. The incremental builder repeats this check as a
    # safety net for direct callers and for free-space changes during parsing.
    chunker.ensure_sufficient_disk_space(
        obj_path,
        target_cache_dir,
        staged_asset_bytes=chunker.cache_assets_size(texture_assets),
    )

    active_chunk_size = (
        float(chunk_size)
        if chunk_size is not None
        else chunker.configured_chunk_size()
    )

    build_options = {
        "progress_cb": lambda stage, frac: _emit_progress(progress_cb, stage, frac),
        "cache_dir": target_cache_dir,
        "assets": texture_assets,
        "pause_requested": pause_requested,
        "chunk_size": active_chunk_size,
        "face_batch_size": obj_import_batch_faces,
        "bucket_workers": obj_bucket_workers,
        "max_upload_group_mb": max_upload_group_mb,
    }
    if resume_required:
        build_options["resume_required"] = True
    return chunker.build_cache_incremental_obj(
        obj_path,
        materials,
        **build_options,
    )


def probe_resumable_import(
    model_descriptor: dict,
    *,
    cache_dir: str | None = None,
    chunk_size: float | None = None,
    obj_import_batch_faces: int | None = None,
) -> chunker.ResumableObjImport | None:
    """Return a validated OBJ checkpoint for one model descriptor, if any.

    This deliberately parses the material file in the core layer so callers
    need neither checkpoint-format knowledge nor MTL parsing logic.
    """
    source_format = source_model.source_format_for_id(
        model_descriptor.get("format")
    )
    if (
        source_format is None
        or source_format.id is not source_model.SourceFormatId.OBJ
    ):
        return None
    obj_path = model_descriptor.get("obj_path")
    mtl_path = model_descriptor.get("mtl_path")
    if not obj_path or not mtl_path:
        return None
    try:
        materials = obj_parser.parse_mtl(mtl_path)
        active_chunk_size = (
            float(chunk_size)
            if chunk_size is not None
            else chunker.configured_chunk_size()
        )
        return chunker.probe_resumable_obj_import(
            obj_path,
            materials,
            cache_dir=cache_dir,
            chunk_size=active_chunk_size,
            face_batch_size=obj_import_batch_faces,
        )
    except (OSError, TypeError, UnicodeError, ValueError):
        return None


def import_and_cache_any(
    model_descriptor: dict,
    textures_dir: str,
    force_rebuild: bool = False,
    *,
    progress_cb: ProgressCallback | None = None,
    pause_requested: PauseCallback | None = None,
    chunk_size: float | None = None,
    cache_dir: str | None = None,
    max_upload_group_mb: float | None = None,
    obj_import_batch_faces: int | None = None,
    obj_bucket_workers: int | None = None,
    resume_required: bool = False,
) -> str:
    """Dispatch a model descriptor to the correct parser/cache path."""
    raw_format = model_descriptor.get("format")
    source_format = source_model.source_format_for_id(raw_format)
    if source_format is None:
        raise ValueError(f"Unknown model format: {raw_format!r}")

    if source_format.id is source_model.SourceFormatId.OBJ:
        import_options = {
            "force_rebuild": force_rebuild,
            "progress_cb": progress_cb,
            "pause_requested": pause_requested,
            "chunk_size": chunk_size,
            "cache_dir": cache_dir,
            "max_upload_group_mb": max_upload_group_mb,
            "obj_import_batch_faces": obj_import_batch_faces,
            "obj_bucket_workers": obj_bucket_workers,
        }
        if resume_required:
            import_options["resume_required"] = True
        return import_and_cache(
            model_descriptor["obj_path"],
            model_descriptor["mtl_path"],
            **import_options,
        )

    if source_format.id is not source_model.SourceFormatId.GLB:
        raise ValueError(f"Unknown model format: {raw_format!r}")

    if resume_required:
        raise ResumeCheckpointUnavailableError()

    source_path = model_descriptor.get(source_format.descriptor_path_key)
    if not source_path:
        raise ValueError(
            f"Missing {source_format.descriptor_path_key} for "
            f"{source_format.id.value!r} model format"
        )

    target_cache_dir = os.path.abspath(cache_dir or map_cache_build_dir(source_path))
    if not force_rebuild and cache_dir is not None:
        if chunker.cache_dir_is_valid(target_cache_dir, source_path):
            _LOG.info(
                "Using an existing chunk cache; remove the reported cache directory "
                "to force a rebuild."
            )
            _LOG.info("Found cache in: %s", target_cache_dir)
            return target_cache_dir
    elif not force_rebuild and chunker.cache_is_valid(source_path):
        cache_dir = chunker.get_cache_dir(source_path)
        _LOG.info(
            "Using an existing chunk cache; remove the reported cache directory "
            "to force a rebuild."
        )
        _LOG.info("Found cache in: %s", cache_dir)
        return cache_dir

    chunker.ensure_sufficient_disk_space(source_path, target_cache_dir)

    active_chunk_size = (
        float(chunk_size)
        if chunk_size is not None
        else chunker.configured_chunk_size()
    )

    parse_weight = 0.5

    def parse_progress(stage: str, frac: float) -> None:
        _emit_progress(progress_cb, stage, parse_weight * frac)

    def cache_progress(stage: str, frac: float) -> None:
        _emit_progress(progress_cb, stage, parse_weight + (1.0 - parse_weight) * frac)

    chunker.ensure_sufficient_source_file_read_memory(source_path)
    mesh, embedded_textures = glb_parser.parse_glb(
        source_path,
        progress_cb=parse_progress,
        preflight_cb=lambda vertex_count, uv_count, normal_count, face_count: (
            chunker.ensure_sufficient_import_memory(
                vertex_count,
                uv_count,
                normal_count,
                face_count,
                source_path=source_path,
            )
        ),
    )
    chunker.ensure_sufficient_import_memory(
        len(getattr(mesh, "positions", ())),
        len(getattr(mesh, "uvs", ())),
        len(getattr(mesh, "normals", ())),
        len(getattr(mesh, "face_pos_idx", ())),
        source_path=source_path,
    )

    # Embedded images become ordinary named cache assets. They remain in the
    # private staging tree until the chunks and manifest are complete, so
    # read-only source folders are supported without exposing an incomplete
    # manifest/textures pair.
    materials = {}
    texture_assets = []
    staged_texture_names = set()
    for mat_range in mesh.material_ranges:
        mat_name = mat_range.material_name
        if mat_name in embedded_textures:
            image_bytes = embedded_textures[mat_name]
            image_filename = embedded_texture_filename(image_bytes, mat_name)
            materials[mat_name] = obj_parser.Material(
                name=mat_name,
                diffuse_texture=image_filename,
            )
            if image_filename not in staged_texture_names:
                texture_assets.append(
                    chunker.CacheAsset(
                        relative_path=image_filename, data=image_bytes
                    )
                )
                staged_texture_names.add(image_filename)
        else:
            materials[mat_name] = obj_parser.Material(
                name=mat_name,
                diffuse_texture=None,
            )

    chunker.ensure_sufficient_disk_space(
        source_path,
        target_cache_dir,
        staged_asset_bytes=chunker.cache_assets_size(texture_assets),
    )
    return chunker.build_cache(
        source_path,
        mesh,
        materials,
        progress_cb=cache_progress,
        cache_dir=target_cache_dir,
        assets=texture_assets,
        chunk_size=active_chunk_size,
        max_upload_group_mb=max_upload_group_mb,
    )


def file_texture_assets(materials: dict, textures_dir: str):
    """Return unique on-disk textures for atomic cache publication."""
    assets = []
    seen_paths = set()
    for material in materials.values():
        relative_path = material.diffuse_texture
        if not relative_path or relative_path in seen_paths:
            continue
        source_path = resolve_texture_path(textures_dir, relative_path)
        if os.path.isfile(source_path):
            assets.append(
                chunker.CacheAsset(relative_path=relative_path, source_path=source_path)
            )
            seen_paths.add(relative_path)
    return assets


def embedded_texture_filename(image_bytes: bytes, material_name: str) -> str:
    """Choose a deterministic extension for an embedded GLB texture."""
    if image_bytes[:2] == b"\xff\xd8":
        ext = ".jpg"
    elif image_bytes[:8] == b"\x89PNG\r\n\x1a\n":
        ext = ".png"
    else:
        ext = ".img"
    return f"{material_name}{ext}"
