"""Feature availability policies and immutable gate decisions for the GUI."""

from .gates import FeatureGateRegistry
from .ids import FeatureId
from .model import FeatureDecision, FeatureState
from .policies import (
    decide_automatic_update,
    decide_directory_selection,
    decide_map_source_import,
    decide_video_recording,
)

__all__ = [
    "FeatureDecision",
    "FeatureGateRegistry",
    "FeatureId",
    "FeatureState",
    "decide_automatic_update",
    "decide_directory_selection",
    "decide_map_source_import",
    "decide_video_recording",
]
