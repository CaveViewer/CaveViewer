"""Feature availability policies and immutable gate decisions for the GUI."""

from .gates import FeatureGateRegistry
from .ids import FeatureId
from .model import FeatureDecision, FeatureState
from .policies import (
    decide_automatic_update,
    decide_desktop_notification,
    decide_directory_selection,
    decide_file_selection,
    decide_guided_dive_playback,
    decide_idle_suspend_inhibition,
    decide_map_source_import,
    decide_update_package_reveal,
    decide_video_recording,
    decide_viewer_launch,
)

__all__ = [
    "FeatureDecision",
    "FeatureGateRegistry",
    "FeatureId",
    "FeatureState",
    "decide_automatic_update",
    "decide_desktop_notification",
    "decide_directory_selection",
    "decide_file_selection",
    "decide_guided_dive_playback",
    "decide_idle_suspend_inhibition",
    "decide_map_source_import",
    "decide_update_package_reveal",
    "decide_video_recording",
    "decide_viewer_launch",
]
