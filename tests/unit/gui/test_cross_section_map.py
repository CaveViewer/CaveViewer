from __future__ import annotations

from unittest.mock import Mock

import numpy as np

from gui.cross_section_map import CrossSectionMap


def test_prime_uploads_initial_profile_before_chunk_streaming():
    profile = object.__new__(CrossSectionMap)
    raw_key = (24.0, 3.0, 0.0)
    segments = [(20.0, -1.0, 28.0, 2.0, 1.0)]
    window_size = (1280, 720)
    profile._active_raw_key = None
    profile._active_segments = []
    profile._uploaded_frame_key = None
    profile._view_for_camera = Mock(return_value=(raw_key, 25.0))
    profile._build_raw_segments = Mock(return_value=segments)
    profile._cache_put = Mock()
    profile._camera_along_for_key = Mock(return_value=25.0)
    profile._display_along = Mock(return_value=24.0)
    profile._build_frame_geom = Mock(return_value=(b"profile geometry", 12))
    profile._upload_geom = Mock()

    camera_position = np.array([25.0, 0.0, 3.0], dtype=np.float32)
    camera_forward = np.array([1.0, 0.0, 0.0], dtype=np.float32)
    profile.prime(camera_position, camera_forward, window_size)

    profile._cache_put.assert_called_once_with(raw_key, segments)
    profile._build_frame_geom.assert_called_once_with(window_size, 24.0, segments)
    profile._upload_geom.assert_called_once_with(b"profile geometry", 12)
    assert profile._active_raw_key == raw_key
    assert profile._active_segments == segments
    assert profile._uploaded_frame_key == (raw_key, window_size, 24.0)
