"""Platform-bound capability probes with no product-policy decisions."""

from .updates import (
    UpdateConfiguration,
    UpdateTarget,
    build_update_configuration,
    probe_automatic_update,
)
from .recording import (
    VideoRecordingTarget,
    probe_recording_output_directory,
    probe_video_recording,
)

__all__ = [
    "UpdateConfiguration",
    "UpdateTarget",
    "VideoRecordingTarget",
    "build_update_configuration",
    "probe_automatic_update",
    "probe_recording_output_directory",
    "probe_video_recording",
]
