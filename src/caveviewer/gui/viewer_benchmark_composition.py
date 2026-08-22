"""Pure benchmark metadata composition extracted from the viewer window."""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping

from caveviewer.core.preferences.runtime_settings import RuntimeSettings


STREAMING_ENV_FIELDS = (
    ("system_ram_target_percent", "CAVEVIEWER_MEMORY_UTILIZATION_TARGET"),
    ("gpu_memory_target_percent", "CAVEVIEWER_GPU_MEMORY_UTILIZATION_TARGET"),
    ("gpu_memory_override_gb", "CAVEVIEWER_GPU_MEMORY_GB"),
    ("texture_resident_cache_mb", "CAVEVIEWER_TEXTURE_RESIDENT_CACHE_MB"),
    ("io_workers", "CAVEVIEWER_IO_WORKERS"),
    ("io_reserved_cpus", "CAVEVIEWER_IO_RESERVED_CPUS"),
    ("upload_chunks_per_frame", "CAVEVIEWER_UPLOAD_CHUNKS_PER_FRAME"),
    ("upload_groups_per_frame", "CAVEVIEWER_UPLOAD_GROUPS_PER_FRAME"),
    ("upload_time_budget_ms", "CAVEVIEWER_UPLOAD_TIME_BUDGET_MS"),
)


def environment_size(value) -> list[int] | None:
    """Return a stable two-item size list for benchmark metadata."""
    try:
        width, height = value
        width = int(width)
        height = int(height)
    except Exception:
        return None
    if width <= 0 or height <= 0:
        return None
    return [width, height]


def streaming_settings_snapshot(
    scenario,
    *,
    runtime_settings: RuntimeSettings | None = None,
    environ: Mapping[str, str] | None = None,
) -> dict[str, object]:
    """Return requested settings that determine benchmark comparability."""
    render_distance = getattr(scenario, "render_distance", "")
    try:
        render_distance = int(render_distance)
    except (TypeError, ValueError):
        render_distance = str(render_distance)
    settings: dict[str, object] = {
        "render_distance_chunks": render_distance,
    }
    if runtime_settings is not None:
        streaming = runtime_settings.streaming_configuration()
        settings.update(
            system_ram_target_percent=streaming.memory_target_percent,
            gpu_memory_target_percent=streaming.gpu_memory_target_percent,
            gpu_memory_override_gb=streaming.gpu_memory_gb or "",
            texture_resident_cache_mb=streaming.texture_resident_cache_mb or "",
            io_workers=streaming.io_workers,
            io_reserved_cpus=streaming.io_reserved_cpus,
            upload_chunks_per_frame=streaming.upload_chunks_per_frame,
            upload_groups_per_frame=streaming.upload_groups_per_frame,
            upload_time_budget_ms=streaming.upload_time_budget_ms,
        )
        return settings
    environment = os.environ if environ is None else environ
    for key, variable in STREAMING_ENV_FIELDS:
        settings[key] = environment.get(variable, "")
    return settings


def streaming_settings_fingerprint(settings: Mapping[str, object]) -> str:
    """Return a deterministic fingerprint for benchmark comparison gates."""
    payload = json.dumps(
        dict(settings), default=str, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()
