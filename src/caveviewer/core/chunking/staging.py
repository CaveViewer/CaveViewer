"""Cache staging, publishing, and incremental import resume checkpoints."""

from __future__ import annotations

import json
import math
import os
import shutil
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from caveviewer.core.chunking.io import CHUNKS_DIRNAME
from caveviewer.core.diagnostics.logging import get_logger
from caveviewer.core.json_io import load_bounded_json


MANIFEST_NAME = "manifest.json"
IMPORT_RESUME_MANIFEST_NAME = "import_resume.json"
_INCREMENTAL_OBJ_RESUME_VERSION = 1
MAX_IMPORT_RESUME_CHECKPOINT_BYTES = 128 * 1024 * 1024
_LOG = get_logger("chunker")


class ImportPaused(RuntimeError):
    """Raised when an import is paused after writing a resume checkpoint."""

    def __init__(self, resume_dir: str | None = None):
        self.resume_dir = resume_dir
        message = "Import paused; resume checkpoint saved."
        if resume_dir:
            message = f"{message} Resume directory: {resume_dir}"
        super().__init__(message)


class ResumeCheckpointUnavailableError(RuntimeError):
    """Raised when an explicitly requested import resume checkpoint is unavailable."""

    def __init__(self) -> None:
        super().__init__("Saved rebuild checkpoint is no longer usable.")


@dataclass(frozen=True, slots=True)
class ResumableObjImport:
    """A validated on-disk checkpoint for one incremental OBJ import."""

    resume_dir: Path
    stage: str
    progress_fraction: float


@dataclass(frozen=True)
class CacheAsset:
    """One texture or other immutable asset published with a map cache."""

    relative_path: str
    source_path: str | None = None
    data: bytes | None = None

    def __post_init__(self) -> None:
        if (self.source_path is None) == (self.data is None):
            raise ValueError(
                "CacheAsset requires exactly one source_path or data value"
            )


def _nearest_existing_directory(path: str) -> str:
    """Find the filesystem that will contain a not-yet-created cache path."""
    candidate = os.path.abspath(path)
    while not os.path.isdir(candidate):
        parent = os.path.dirname(candidate)
        if parent == candidate:
            break
        candidate = parent
    return candidate


def _cache_asset_size(asset: CacheAsset) -> int:
    if asset.source_path is not None:
        return os.path.getsize(asset.source_path)
    return len(asset.data or b"")


def _stage_cache_assets(
    staging_dir: str, assets: tuple[CacheAsset, ...] | list[CacheAsset]
) -> None:
    """Write validated relative assets inside an unpublished cache tree."""
    written_paths: set[str] = set()
    for asset in assets:
        relative_path = os.path.normpath(asset.relative_path)
        first_component = relative_path.split(os.sep, 1)[0]
        if (
            not relative_path
            or os.path.isabs(relative_path)
            or relative_path == os.pardir
            or relative_path.startswith(os.pardir + os.sep)
            or first_component in {CHUNKS_DIRNAME, MANIFEST_NAME}
        ):
            raise ValueError(f"Unsafe cache asset path: {asset.relative_path!r}")
        if relative_path in written_paths:
            raise ValueError(f"Duplicate cache asset path: {asset.relative_path!r}")
        written_paths.add(relative_path)

        destination = os.path.join(staging_dir, relative_path)
        os.makedirs(os.path.dirname(destination), exist_ok=True)
        if asset.source_path is not None:
            shutil.copy2(asset.source_path, destination)
        else:
            with open(destination, "wb") as output:
                output.write(asset.data or b"")


def _publish_cache_directory(staging_dir: str, cache_dir: str) -> None:
    """Publish a completed staging tree while preserving an old cache on failure."""
    backup_dir = f"{staging_dir}.previous"
    moved_existing_cache = False

    try:
        if os.path.lexists(cache_dir):
            os.replace(cache_dir, backup_dir)
            moved_existing_cache = True
        os.replace(staging_dir, cache_dir)
    except BaseException:
        if moved_existing_cache:
            try:
                os.replace(backup_dir, cache_dir)
            except OSError as restore_error:
                _LOG.error(
                    "Could not restore previous cache %s after publish failure: %s",
                    cache_dir,
                    restore_error,
                )
        raise

    if moved_existing_cache:
        try:
            shutil.rmtree(backup_dir)
        except OSError as cleanup_error:
            _LOG.warning(
                "Could not remove replaced cache backup %s: %s",
                backup_dir,
                cleanup_error,
            )


def _import_resume_prefix(cache_dir: str) -> str:
    return f".{os.path.basename(cache_dir)}.resume-"


def _import_resume_checkpoint_path(staging_dir: str) -> str:
    return os.path.join(staging_dir, IMPORT_RESUME_MANIFEST_NAME)


def _source_resume_identity(source_path: str) -> dict:
    stat = os.stat(source_path)
    return {
        "path": os.path.abspath(source_path),
        "size": int(stat.st_size),
        "mtime_ns": int(stat.st_mtime_ns),
    }


def _materials_resume_identity(materials: dict) -> dict[str, str | None]:
    return {
        str(name): getattr(material, "diffuse_texture", None)
        for name, material in sorted(materials.items(), key=lambda item: str(item[0]))
    }


def _atomic_write_json(path: str, payload: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        prefix=f".{os.path.basename(path)}.",
        suffix=".tmp",
        dir=os.path.dirname(path),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            json.dump(payload, output, sort_keys=True)
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except FileNotFoundError:
            pass
        raise


def _resolve_resume_relative_path(root_dir: str, relative_path: str) -> str:
    raw_path = str(relative_path)
    normalized = os.path.normpath(raw_path)
    if (
        not raw_path.strip()
        or os.path.isabs(normalized)
        or normalized == os.pardir
        or normalized.startswith(os.pardir + os.sep)
    ):
        raise ValueError(f"Unsafe resume bucket path: {raw_path!r}")

    root = os.path.abspath(root_dir)
    resolved = os.path.abspath(os.path.join(root, normalized))
    try:
        common = os.path.commonpath((root, resolved))
    except ValueError as exc:
        raise ValueError(f"Unsafe resume bucket path: {raw_path!r}") from exc
    if common != root:
        raise ValueError(f"Unsafe resume bucket path: {raw_path!r}")
    return resolved


def _serialize_bucket_parts(
    bucket_parts: dict[tuple[tuple[int, int, int], str], list[str]],
    root_dir: str,
) -> list[dict]:
    serialized = []
    for (cell, material_name), paths in sorted(
        bucket_parts.items(),
        key=lambda item: (item[0][0], item[0][1]),
    ):
        serialized.append(
            {
                "cell": [int(cell[0]), int(cell[1]), int(cell[2])],
                "material": str(material_name),
                "paths": [
                    os.path.relpath(path, root_dir)
                    for path in paths
                ],
            }
        )
    return serialized


def _deserialize_bucket_parts(
    payload: list[dict],
    root_dir: str,
) -> dict[tuple[tuple[int, int, int], str], list[str]]:
    bucket_parts: dict[tuple[tuple[int, int, int], str], list[str]] = {}
    for item in payload:
        if not isinstance(item, dict):
            raise ValueError("Resume checkpoint contains an invalid bucket entry.")
        cell_payload = item.get("cell", [])
        if not isinstance(cell_payload, (list, tuple)) or len(cell_payload) != 3:
            raise ValueError("Resume checkpoint contains an invalid bucket cell.")
        cell = (
            int(cell_payload[0]),
            int(cell_payload[1]),
            int(cell_payload[2]),
        )
        material_name = str(item.get("material", "__no_material__"))
        paths_payload = item.get("paths", [])
        if not isinstance(paths_payload, list):
            raise ValueError("Resume checkpoint contains invalid bucket paths.")
        paths = [
            _resolve_resume_relative_path(root_dir, str(relative_path))
            for relative_path in paths_payload
        ]
        bucket_parts[(cell, material_name)] = paths
    return bucket_parts


def _write_incremental_obj_resume_checkpoint(
    staging_dir: str,
    *,
    obj_path: str,
    materials: dict,
    chunk_size: float,
    face_batch_size: int,
    stage: str,
    next_batch_index: int,
    bucketed_faces: int,
    face_count: int,
    bucket_parts: dict[tuple[tuple[int, int, int], str], list[str]],
    progress_fraction: float,
    completed_manifest_chunks: dict | None = None,
    total_cell_count: int | None = None,
) -> None:
    payload = {
        "version": _INCREMENTAL_OBJ_RESUME_VERSION,
        "kind": "incremental_obj_import",
        "source": _source_resume_identity(obj_path),
        "chunk_size": float(chunk_size),
        "face_batch_size": int(face_batch_size),
        "materials": _materials_resume_identity(materials),
        "stage": str(stage),
        "next_batch_index": int(next_batch_index),
        "bucketed_faces": int(bucketed_faces),
        "face_count": int(face_count),
        "progress_fraction": max(0.0, min(1.0, float(progress_fraction))),
        "bucket_parts": _serialize_bucket_parts(bucket_parts, staging_dir),
        "completed_manifest_chunks": completed_manifest_chunks or {},
        "total_cell_count": (
            None if total_cell_count is None else int(total_cell_count)
        ),
        "updated_at": time.time(),
    }
    _atomic_write_json(_import_resume_checkpoint_path(staging_dir), payload)


def _read_incremental_obj_resume_checkpoint(path: str) -> dict | None:
    try:
        checkpoint = load_bounded_json(
            path,
            max_bytes=MAX_IMPORT_RESUME_CHECKPOINT_BYTES,
            description="import resume checkpoint",
        )
    except (OSError, ValueError):
        return None
    if not isinstance(checkpoint, dict):
        return None
    return checkpoint


def _is_nonnegative_checkpoint_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _checkpoint_fraction(value: Any) -> float | None:
    try:
        fraction = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(fraction) or not 0.0 <= fraction <= 1.0:
        return None
    return fraction


def _checkpoint_timestamp(value: Any) -> float | None:
    try:
        timestamp = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(timestamp):
        return None
    return timestamp


def _checkpoint_chunk_key_is_valid(value: Any) -> bool:
    """Return whether a completed manifest key names one canonical chunk."""
    if not isinstance(value, str):
        return False
    fields = value.split("_")
    if len(fields) != 3:
        return False
    try:
        coordinates = tuple(int(field) for field in fields)
    except ValueError:
        return False
    return value == "_".join(str(coordinate) for coordinate in coordinates)


def _completed_resume_chunks_are_usable(
    checkpoint: dict,
    *,
    resume_dir: str,
) -> bool:
    """Verify finalization checkpoints retain every already-written chunk."""
    completed_chunks = checkpoint["completed_manifest_chunks"]
    if checkpoint.get("stage") == "bucketing":
        return not completed_chunks

    total_cell_count = checkpoint.get("total_cell_count")
    if total_cell_count is not None and len(completed_chunks) > total_cell_count:
        return False
    for cell_key, metadata in completed_chunks.items():
        if not _checkpoint_chunk_key_is_valid(cell_key) or not isinstance(
            metadata, dict
        ):
            return False
        chunk_path = os.path.join(
            resume_dir,
            CHUNKS_DIRNAME,
            f"{cell_key}.bin",
        )
        if not os.path.isfile(chunk_path):
            return False
    return True


def _incremental_obj_resume_checkpoint_is_usable(
    checkpoint: dict,
    *,
    resume_dir: str,
) -> bool:
    """Reject malformed checkpoint payloads before offering or reusing them."""
    if _checkpoint_fraction(checkpoint.get("progress_fraction")) is None:
        return False
    if _checkpoint_timestamp(checkpoint.get("updated_at")) is None:
        return False
    if not all(
        _is_nonnegative_checkpoint_int(checkpoint.get(field))
        for field in ("next_batch_index", "bucketed_faces", "face_count")
    ):
        return False
    if not isinstance(checkpoint.get("bucket_parts"), list):
        return False
    if not isinstance(checkpoint.get("completed_manifest_chunks"), dict):
        return False
    total_cell_count = checkpoint.get("total_cell_count")
    if total_cell_count is not None and not _is_nonnegative_checkpoint_int(
        total_cell_count
    ):
        return False
    if not _completed_resume_chunks_are_usable(
        checkpoint,
        resume_dir=resume_dir,
    ):
        return False
    if checkpoint["bucketed_faces"] > checkpoint["face_count"]:
        return False

    try:
        bucket_parts = _deserialize_bucket_parts(
            checkpoint["bucket_parts"],
            resume_dir,
        )
    except (TypeError, ValueError, OSError):
        return False
    return all(
        os.path.isfile(path)
        for paths in bucket_parts.values()
        for path in paths
    )


def _incremental_obj_resume_checkpoint_matches(
    checkpoint: dict,
    *,
    obj_path: str,
    materials: dict,
    chunk_size: float,
    face_batch_size: int,
) -> bool:
    if checkpoint.get("version") != _INCREMENTAL_OBJ_RESUME_VERSION:
        return False
    if checkpoint.get("kind") != "incremental_obj_import":
        return False
    if checkpoint.get("stage") not in {"bucketing", "finalizing"}:
        return False
    try:
        checkpoint_chunk_size = float(checkpoint.get("chunk_size", -1.0))
        expected_chunk_size = float(chunk_size)
    except (TypeError, ValueError, OverflowError):
        return False
    if (
        not math.isfinite(checkpoint_chunk_size)
        or checkpoint_chunk_size != expected_chunk_size
    ):
        return False
    if not _is_nonnegative_checkpoint_int(checkpoint.get("face_batch_size")):
        return False
    if checkpoint["face_batch_size"] != int(face_batch_size):
        return False
    if checkpoint.get("materials") != _materials_resume_identity(materials):
        return False
    try:
        return checkpoint.get("source") == _source_resume_identity(obj_path)
    except OSError:
        return False


def _find_incremental_obj_resume(
    cache_dir: str,
    *,
    obj_path: str,
    materials: dict,
    chunk_size: float,
    face_batch_size: int,
) -> tuple[str, dict] | None:
    cache_parent = os.path.dirname(cache_dir)
    prefix = _import_resume_prefix(cache_dir)
    try:
        names = os.listdir(cache_parent)
    except OSError:
        return None

    candidates: list[tuple[float, str, dict]] = []
    for name in names:
        if not name.startswith(prefix):
            continue
        resume_dir = os.path.join(cache_parent, name)
        if not os.path.isdir(resume_dir):
            continue
        checkpoint_path = _import_resume_checkpoint_path(resume_dir)
        checkpoint = _read_incremental_obj_resume_checkpoint(checkpoint_path)
        if checkpoint is None:
            continue
        if not _incremental_obj_resume_checkpoint_matches(
            checkpoint,
            obj_path=obj_path,
            materials=materials,
            chunk_size=chunk_size,
            face_batch_size=face_batch_size,
        ):
            continue
        if not _incremental_obj_resume_checkpoint_is_usable(
            checkpoint,
            resume_dir=resume_dir,
        ):
            continue
        updated_at = _checkpoint_timestamp(checkpoint.get("updated_at"))
        if updated_at is None:
            continue
        candidates.append(
            (
                updated_at,
                resume_dir,
                checkpoint,
            )
        )

    if not candidates:
        return None
    _updated_at, resume_dir, checkpoint = max(candidates, key=lambda item: item[0])
    return resume_dir, checkpoint


def find_resumable_obj_import(
    cache_dir: str,
    *,
    obj_path: str,
    materials: dict,
    chunk_size: float,
    face_batch_size: int,
) -> ResumableObjImport | None:
    """Return the newest fully validated OBJ checkpoint for this build target."""
    resume = _find_incremental_obj_resume(
        cache_dir,
        obj_path=obj_path,
        materials=materials,
        chunk_size=chunk_size,
        face_batch_size=face_batch_size,
    )
    if resume is None:
        return None
    resume_dir, checkpoint = resume
    fraction = _checkpoint_fraction(checkpoint.get("progress_fraction"))
    if fraction is None:
        return None
    return ResumableObjImport(
        resume_dir=Path(resume_dir),
        stage=str(checkpoint["stage"]),
        progress_fraction=fraction,
    )


def _preserve_resumable_import(staging_dir: str, cache_dir: str) -> str:
    cache_parent = os.path.dirname(cache_dir)
    prefix = _import_resume_prefix(cache_dir)
    if os.path.basename(staging_dir).startswith(prefix):
        return staging_dir

    for attempt in range(1000):
        suffix = f"{os.getpid()}-{time.time_ns()}"
        if attempt:
            suffix = f"{suffix}-{attempt}"
        resume_dir = os.path.join(cache_parent, f"{prefix}{suffix}")
        if not os.path.exists(resume_dir):
            os.replace(staging_dir, resume_dir)
            return resume_dir
    raise RuntimeError("Could not allocate a paused import resume directory.")


def _remove_resume_checkpoint(staging_dir: str) -> None:
    try:
        os.remove(_import_resume_checkpoint_path(staging_dir))
    except FileNotFoundError:
        pass
