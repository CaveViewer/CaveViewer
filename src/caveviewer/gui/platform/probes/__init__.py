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
from .desktop import probe_directory_selection

__all__ = [
    "UpdateConfiguration",
    "UpdateTarget",
    "VideoRecordingTarget",
    "build_update_configuration",
    "probe_automatic_update",
    "probe_directory_selection",
    "probe_recording_output_directory",
    "probe_video_recording",
]
