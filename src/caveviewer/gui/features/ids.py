"""Stable identifiers for user-visible GUI features."""

from __future__ import annotations

from enum import Enum


class FeatureId(str, Enum):
    """Feature keys used by policy, presentation, and diagnostics."""

    AUTOMATIC_UPDATE = "automatic_update"
    DIRECTORY_SELECTION = "directory_selection"
    GUIDED_DIVE_PLAYBACK = "guided_dive_playback"
    MAP_SOURCE_IMPORT = "map_source_import"
    UPDATE_PACKAGE_REVEAL = "update_package_reveal"
    VIDEO_RECORDING = "video_recording"
