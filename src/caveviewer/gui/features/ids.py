"""Stable identifiers for user-visible GUI features."""

from __future__ import annotations

from enum import Enum


class FeatureId(str, Enum):
    """Feature keys used by policy, presentation, and diagnostics."""

    AUTOMATIC_UPDATE = "automatic_update"
    DIRECTORY_SELECTION = "directory_selection"
    MAP_SOURCE_IMPORT = "map_source_import"
    VIDEO_RECORDING = "video_recording"
