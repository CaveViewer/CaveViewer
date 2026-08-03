"""On-demand video-recording capability probes at the platform edge."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
import os
import tempfile

from caveviewer.core.capabilities import (
    CapabilityResult,
    CapabilitySource,
    CapabilityStatus,
)
from caveviewer.gui import recording


FfmpegResolver = Callable[[], str | None]
RecordingOutputProbe = Callable[[str], CapabilityResult[str]]


@dataclass(frozen=True, slots=True)
class VideoRecordingTarget:
    """The encoder and writable directory selected for one recording start."""

    ffmpeg_path: str
    output_directory: str


def _recording_configuration_source(environment: Mapping[str, str]) -> CapabilitySource:
    """Identify whether an explicit recording environment setting was supplied."""
    if any(
        str(environment.get(name, "")).strip()
        for name in ("CAVEVIEWER_FFMPEG", "CAVEVIEWER_RECORDING_DIR")
    ):
        return CapabilitySource.USER_OVERRIDE
    return CapabilitySource.DETECTED


def probe_recording_output_directory(output_directory: str) -> CapabilityResult[str]:
    """Confirm that a recording directory can be created and written on demand.

    The short-lived temporary file is the reliable cross-platform write check.
    It is created only after the user requests recording and is removed by the
    operating system when its handle closes.
    """
    raw_directory = str(output_directory).strip()
    if not raw_directory:
        return CapabilityResult.unavailable(
            reason_code="video_recording_output_directory_unavailable",
            evidence={"output_directory": "empty"},
        )

    try:
        directory = os.path.abspath(os.path.expanduser(raw_directory))
        os.makedirs(directory, exist_ok=True)
        with tempfile.TemporaryFile(prefix=".caveviewer-recording-", dir=directory):
            pass
    except (OSError, ValueError):
        return CapabilityResult.unavailable(
            reason_code="video_recording_output_directory_unavailable",
            evidence={"output_directory": "unwritable"},
        )

    return CapabilityResult.available(
        directory,
        reason_code="video_recording_output_directory_available",
        evidence={"output_directory": "writable"},
    )


def probe_video_recording(
    output_directory: str,
    *,
    ffmpeg_resolver: FfmpegResolver | None = None,
    output_directory_probe: RecordingOutputProbe = probe_recording_output_directory,
    environment: Mapping[str, str] | None = None,
) -> CapabilityResult[VideoRecordingTarget]:
    """Report whether ffmpeg and a writable destination are ready to record.

    This probe intentionally performs no work until the recording action asks
    for it. It validates only preconditions that can be known before capture;
    OpenGL readback resources remain a render-thread concern at encoder start.
    """
    values = os.environ if environment is None else environment
    source = _recording_configuration_source(values)
    resolve = ffmpeg_resolver or recording.resolve_ffmpeg_path
    try:
        ffmpeg_path = resolve()
    except Exception:
        return CapabilityResult.unknown(
            reason_code="video_recording_encoder_probe_failed",
            source=source,
            evidence={"encoder": "ffmpeg"},
        )

    normalized_ffmpeg_path = str(ffmpeg_path).strip() if ffmpeg_path else ""
    if not normalized_ffmpeg_path:
        return CapabilityResult.unavailable(
            reason_code="video_recording_encoder_unavailable",
            source=source,
            evidence={"encoder": "ffmpeg"},
        )

    try:
        directory_result = output_directory_probe(output_directory)
    except Exception:
        return CapabilityResult.unknown(
            reason_code="video_recording_output_directory_probe_failed",
            source=source,
            evidence={"output_directory": "probe_failed"},
        )

    if directory_result.status is not CapabilityStatus.AVAILABLE:
        return CapabilityResult(
            status=directory_result.status,
            value=None,
            source=source,
            reason_code=directory_result.reason_code,
            evidence=directory_result.evidence,
        )
    if not directory_result.value:
        return CapabilityResult.unknown(
            reason_code="video_recording_output_directory_probe_failed",
            source=source,
            evidence={"output_directory": "missing_value"},
        )

    return CapabilityResult.available(
        VideoRecordingTarget(
            ffmpeg_path=normalized_ffmpeg_path,
            output_directory=directory_result.value,
        ),
        reason_code="video_recording_target_available",
        source=source,
        evidence={"encoder": "ffmpeg", "output_directory": "writable"},
    )
