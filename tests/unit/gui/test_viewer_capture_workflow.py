"""Unit coverage for non-OpenGL capture workflow coordination."""

from __future__ import annotations

import pytest

from caveviewer.gui.viewer_capture_workflow import (
    CaptureOverlayMode,
    CaptureOverlayState,
    ViewerCaptureWorkflow,
)


def test_exit_finalization_waits_for_artifacts_and_visible_status():
    workflow = ViewerCaptureWorkflow(exit_status_minimum_seconds=0.75)

    assert not workflow.can_complete_exit_finalization(
        artifacts_pending=False,
        now=10.0,
    )

    workflow.begin_exit_finalization()
    assert workflow.exit_finalization_active
    assert not workflow.can_complete_exit_finalization(
        artifacts_pending=True,
        now=10.0,
    )
    assert not workflow.can_complete_exit_finalization(
        artifacts_pending=False,
        now=10.0,
    )

    workflow.mark_exit_status_presented(now=10.0)
    assert not workflow.can_complete_exit_finalization(
        artifacts_pending=False,
        now=10.749,
    )
    assert workflow.can_complete_exit_finalization(
        artifacts_pending=False,
        now=10.75,
    )


def test_iconified_finalization_can_finish_without_a_presented_status():
    workflow = ViewerCaptureWorkflow()
    workflow.begin_exit_finalization()

    assert workflow.can_complete_exit_finalization(
        artifacts_pending=False,
        now=10.0,
        allow_unpresented_status=True,
    )


@pytest.mark.parametrize(
    ("state", "expected"),
    (
        (
            CaptureOverlayState(True, True, True),
            CaptureOverlayMode.RECORDING,
        ),
        (
            CaptureOverlayState(False, True, True),
            CaptureOverlayMode.MANUAL_DIVE_TRACE_COUNTDOWN,
        ),
        (
            CaptureOverlayState(False, False, True),
            CaptureOverlayMode.SLICE_COUNTDOWN,
        ),
        (
            CaptureOverlayState(False, False, False),
            CaptureOverlayMode.HUD,
        ),
    ),
)
def test_overlay_mode_preserves_capture_priority(state, expected):
    assert ViewerCaptureWorkflow.overlay_mode_for(state) is expected
