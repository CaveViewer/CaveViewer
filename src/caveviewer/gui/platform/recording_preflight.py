"""Build typed video-recording preflights at the GUI boundary."""

from __future__ import annotations

from typing import TYPE_CHECKING

from caveviewer.gui.features import decide_video_recording

from .probes.recording import FfmpegResolver, probe_video_recording
from .runtime import VideoRecordingPreflight

if TYPE_CHECKING:
    from .runtime import PlatformRuntime


def video_recording_preflight(
    output_directory: str,
    *,
    ffmpeg_resolver: FfmpegResolver | None = None,
    platform_runtime: PlatformRuntime | None = None,
) -> VideoRecordingPreflight:
    """Return one fresh recording capability paired with its policy decision.

    The process-owned runtime supplies its on-demand preflight for normal GUI
    flows. Compatibility callers use the same side-effect-free probe and pure
    policy through this boundary instead of composing them themselves. Neither
    path starts ffmpeg or allocates render-thread capture resources.
    """
    if platform_runtime is not None:
        runtime_preflight = getattr(
            platform_runtime,
            "video_recording_preflight",
            None,
        )
        if callable(runtime_preflight):
            return runtime_preflight(
                output_directory,
                ffmpeg_resolver=ffmpeg_resolver,
            )

    capability = probe_video_recording(
        output_directory,
        ffmpeg_resolver=ffmpeg_resolver,
    )
    return VideoRecordingPreflight(
        capability=capability,
        decision=decide_video_recording(capability),
    )
