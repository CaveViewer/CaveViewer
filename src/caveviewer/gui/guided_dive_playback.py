"""Action-time Guided Dive discovery, cache preflight, and launch targets.

This module owns the map-specific capability facts used by the splash Map
Library. It intentionally does not enter ``PlatformRuntime.feature_gates``:
the existence and compatibility of a trace can change for each map while the
application is running.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path

from caveviewer.core.capabilities import (
    CapabilityResult,
    CapabilityStatus,
)
from caveviewer.core.chunking import builder as chunker
from caveviewer.gui.features import (
    FeatureDecision,
    FeatureId,
    decide_guided_dive_playback,
)
from caveviewer.gui.features.preflight import validate_route_preflight
from caveviewer.gui.manual_dive_trace import (
    MANUAL_DIVE_TRACE_DIRECTORY,
    manual_dive_trace_directory,
)
from caveviewer.gui.recorded_dive import (
    RECORDED_DIVE_FILE_SUFFIX,
    RecordedDiveError,
    RecordedDiveMapError,
    RecordedDiveTrace,
    load_recorded_dive_trace,
    resolve_recorded_dive_source_path,
    validate_recorded_dive_manifest,
)


@dataclass(frozen=True, slots=True)
class GuidedDivePlaybackTarget:
    """One validated map-local trace and the cache it may safely play against."""

    map_root: Path
    trace: RecordedDiveTrace
    source_path: Path
    cache_dir: Path

    @property
    def route_key(self) -> str:
        """Return the only playback route this validated target authorizes."""
        return "map_local_trace"


@dataclass(frozen=True, slots=True)
class GuidedDivePlaybackPreflight:
    """One selected-trace capability fact paired with its policy decision."""

    capability: CapabilityResult[GuidedDivePlaybackTarget]
    decision: FeatureDecision

    def __post_init__(self) -> None:
        validate_route_preflight(
            capability=self.capability,
            decision=self.decision,
            expected_feature=FeatureId.GUIDED_DIVE_PLAYBACK,
            target_type=GuidedDivePlaybackTarget,
            route_for_target=lambda target: target.route_key,
            feature_label="Guided Dive",
            target_label="Guided Dive playback target",
            decision_label="Guided Dive playback",
        )


def guided_dive_trace_directory(
    map_path: str | os.PathLike[str],
) -> Path:
    """Return the canonical map-local directory used for Guided Dive files."""
    return manual_dive_trace_directory(map_path)


def probe_guided_dive_trace_directory(
    map_path: str | os.PathLike[str],
) -> CapabilityResult[Path]:
    """Report whether a map currently contains a completed Guided Dive file.

    Discovery intentionally checks only for immediate JSONL children. It does
    not parse every trace merely to decide whether the overflow menu should be
    present; a selected trace receives full bounded validation immediately
    before launch instead.
    """
    try:
        trace_directory = guided_dive_trace_directory(map_path)
    except (OSError, TypeError, ValueError):
        return CapabilityResult.unknown(
            reason_code="guided_dive_trace_directory_probe_failed",
            evidence={"trace_directory": "invalid_map_root"},
        )

    try:
        if not trace_directory.is_dir():
            return CapabilityResult.unavailable(
                reason_code="guided_dive_trace_unavailable",
                evidence={"trace_directory": "missing"},
            )
        for entry in trace_directory.iterdir():
            if entry.is_file() and entry.suffix.lower() == RECORDED_DIVE_FILE_SUFFIX:
                return CapabilityResult.available(
                    trace_directory,
                    reason_code="guided_dive_trace_available",
                    evidence={"trace_directory": "contains_jsonl"},
                )
    except OSError:
        return CapabilityResult.unknown(
            reason_code="guided_dive_trace_directory_probe_failed",
            evidence={"trace_directory": "unreadable"},
        )

    return CapabilityResult.unavailable(
        reason_code="guided_dive_trace_unavailable",
        evidence={"trace_directory": "empty"},
    )


def guided_dive_menu_decision(
    map_path: str | os.PathLike[str],
) -> FeatureDecision:
    """Return the fresh presentation decision for one map-library overflow."""
    return decide_guided_dive_playback(probe_guided_dive_trace_directory(map_path))


def probe_guided_dive_playback(
    map_path: str | os.PathLike[str],
    trace_path: str | os.PathLike[str],
) -> CapabilityResult[GuidedDivePlaybackTarget]:
    """Confirm a selected trace belongs to this map and its current cache.

    The returned target is evidence from one action-time snapshot. Startup
    still validates the trace again at the viewer boundary, so a filesystem
    change after this preflight cannot turn the UI decision into an unsafe
    playback launch.
    """
    directory_capability = probe_guided_dive_trace_directory(map_path)
    if directory_capability.status is not CapabilityStatus.AVAILABLE:
        return CapabilityResult(
            status=directory_capability.status,
            value=None,
            source=directory_capability.source,
            reason_code=directory_capability.reason_code,
            evidence=directory_capability.evidence,
        )
    trace_directory = directory_capability.value
    if trace_directory is None:
        return CapabilityResult.unknown(
            reason_code="guided_dive_trace_directory_probe_failed",
            evidence={"trace_directory": "missing_value"},
        )

    try:
        selected_trace = Path(trace_path).expanduser().resolve()
    except (OSError, TypeError, ValueError):
        return CapabilityResult.unknown(
            reason_code="guided_dive_trace_probe_failed",
            evidence={"trace": "path_unreadable"},
        )
    if (
        selected_trace.parent != trace_directory
        or selected_trace.suffix.lower() != RECORDED_DIVE_FILE_SUFFIX
    ):
        return CapabilityResult.unavailable(
            reason_code="guided_dive_trace_not_map_local",
            evidence={"trace": "outside_map_directory"},
        )
    try:
        if not selected_trace.is_file():
            return CapabilityResult.unavailable(
                reason_code="guided_dive_trace_missing",
                evidence={"trace": "missing"},
            )
    except OSError:
        return CapabilityResult.unknown(
            reason_code="guided_dive_trace_probe_failed",
            evidence={"trace": "unreadable"},
        )

    try:
        trace = load_recorded_dive_trace(selected_trace)
    except RecordedDiveError:
        return CapabilityResult.unavailable(
            reason_code="guided_dive_trace_invalid",
            evidence={"trace": "invalid"},
        )
    except Exception:
        return CapabilityResult.unknown(
            reason_code="guided_dive_trace_probe_failed",
            evidence={"trace": "validation_failed"},
        )

    try:
        map_root = trace_directory.parent.resolve()
        source_path = resolve_recorded_dive_source_path(trace)
    except RecordedDiveMapError:
        return CapabilityResult.unavailable(
            reason_code="guided_dive_source_unavailable",
            evidence={"source": "not_found"},
        )
    except (OSError, TypeError, ValueError):
        return CapabilityResult.unknown(
            reason_code="guided_dive_source_probe_failed",
            evidence={"source": "unreadable"},
        )
    if source_path.parent != map_root:
        return CapabilityResult.unavailable(
            reason_code="guided_dive_source_not_map_local",
            evidence={"source": "outside_map_root"},
        )

    try:
        if not chunker.cache_is_valid(os.fspath(source_path)):
            return CapabilityResult.unavailable(
                reason_code="guided_dive_cache_unavailable",
                evidence={"cache": "missing_or_stale"},
            )
        cache_dir = Path(chunker.get_cache_dir(os.fspath(source_path))).resolve()
        manifest = chunker.load_manifest(os.fspath(cache_dir))
    except Exception:
        return CapabilityResult.unknown(
            reason_code="guided_dive_cache_probe_failed",
            evidence={"cache": "probe_failed"},
        )
    if manifest is None:
        return CapabilityResult.unavailable(
            reason_code="guided_dive_cache_unavailable",
            evidence={"cache": "manifest_missing"},
        )
    try:
        validate_recorded_dive_manifest(trace, manifest)
    except RecordedDiveMapError:
        return CapabilityResult.unavailable(
            reason_code="guided_dive_cache_incompatible",
            evidence={"cache": "identity_mismatch"},
        )
    except Exception:
        return CapabilityResult.unknown(
            reason_code="guided_dive_cache_probe_failed",
            evidence={"cache": "validation_failed"},
        )

    return CapabilityResult.available(
        GuidedDivePlaybackTarget(
            map_root=map_root,
            trace=trace,
            source_path=source_path,
            cache_dir=cache_dir,
        ),
        reason_code="guided_dive_playback_target_available",
        evidence={
            "trace_directory": MANUAL_DIVE_TRACE_DIRECTORY,
            "cache": "compatible",
        },
    )


def guided_dive_playback_preflight(
    map_path: str | os.PathLike[str],
    trace_path: str | os.PathLike[str],
) -> GuidedDivePlaybackPreflight:
    """Pair one selected Guided Dive probe with the pure policy decision."""
    capability = probe_guided_dive_playback(map_path, trace_path)
    return GuidedDivePlaybackPreflight(
        capability=capability,
        decision=decide_guided_dive_playback(capability),
    )
