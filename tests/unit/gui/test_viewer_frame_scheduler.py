"""Unit coverage for non-OpenGL viewer frame scheduling."""

from __future__ import annotations

import pytest

from caveviewer.gui.viewer_frame_scheduler import (
    ViewerFramePhase,
    ViewerFrameScheduler,
    ViewerFrameState,
)


@pytest.mark.parametrize(
    ("state", "expected"),
    (
        (
            ViewerFrameState(False, False, False, False, False, False),
            ViewerFramePhase.INACTIVE,
        ),
        (
            ViewerFrameState(True, True, False, False, False, True),
            ViewerFramePhase.INACTIVE,
        ),
        (
            ViewerFrameState(True, False, True, True, True, True),
            ViewerFramePhase.ICONIFIED,
        ),
        (
            ViewerFrameState(True, False, False, True, True, True),
            ViewerFramePhase.FINALIZING_CAPTURE,
        ),
        (
            ViewerFrameState(True, False, False, False, True, True),
            ViewerFramePhase.IMPORTING,
        ),
        (
            ViewerFrameState(True, False, False, False, False, False),
            ViewerFramePhase.STARTUP,
        ),
        (
            ViewerFrameState(True, False, False, False, False, True),
            ViewerFramePhase.INTERACTIVE,
        ),
    ),
)
def test_phase_for_uses_the_viewer_session_priority_order(state, expected):
    assert ViewerFrameScheduler.phase_for(state) is expected


def test_throttle_is_nonblocking_and_resets_when_a_phase_ends():
    scheduler = ViewerFrameScheduler()

    assert scheduler.is_due("iconified", 0.12, now=10.0)
    assert not scheduler.is_due("iconified", 0.12, now=10.119)
    assert scheduler.is_due("iconified", 0.12, now=10.12)

    scheduler.reset_throttle("iconified")

    assert scheduler.is_due("iconified", 0.12, now=10.121)
