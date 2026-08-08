"""Platform-bound capability probes with no product-policy decisions."""

from .updates import (
    UpdateConfiguration,
    UpdateManifestSchema,
    UpdateProfile,
    UpdateTarget,
    build_update_configuration,
    detect_update_package_kind,
    probe_automatic_update,
    select_update_profile,
)
from .recording import (
    VideoRecordingTarget,
    probe_recording_output_directory,
    probe_video_recording,
)
from .desktop import probe_directory_selection
from .update_package_reveal import probe_update_package_reveal

__all__ = [
    "UpdateConfiguration",
    "UpdateManifestSchema",
    "UpdateProfile",
    "UpdateTarget",
    "VideoRecordingTarget",
    "build_update_configuration",
    "detect_update_package_kind",
    "probe_automatic_update",
    "probe_directory_selection",
    "probe_update_package_reveal",
    "probe_recording_output_directory",
    "probe_video_recording",
    "select_update_profile",
]
