"""Unit coverage for non-OpenGL capture workflow coordination."""

from __future__ import annotations

import pytest

from caveviewer.gui.viewer_capture_workflow import (
    CaptureOwner,
    CaptureOverlayMode,
    CaptureOverlayState,
    CaptureOwnershipState,
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


def test_escape_cancellation_close_waits_for_cleanup_and_confirmation_deadline():
    workflow = ViewerCaptureWorkflow()

    assert not workflow.close_pending
    assert not workflow.can_complete_escape_cancellation(
        artifacts_pending=False,
        confirmation_until=15.0,
        now=15.0,
    )

    workflow.begin_escape_cancellation()

    assert workflow.escape_cancellation_active
    assert workflow.close_pending
    assert not workflow.exit_finalization_active
    assert not workflow.can_complete_escape_cancellation(
        artifacts_pending=True,
        confirmation_until=15.0,
        now=15.0,
    )
    assert not workflow.can_complete_escape_cancellation(
        artifacts_pending=False,
        confirmation_until=None,
        now=15.0,
    )
    assert not workflow.can_complete_escape_cancellation(
        artifacts_pending=False,
        confirmation_until=15.0,
        now=14.999,
    )
    assert workflow.can_complete_escape_cancellation(
        artifacts_pending=False,
        confirmation_until=15.0,
        now=15.0,
    )

    workflow.complete_close_workflows()

    assert not workflow.close_pending


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


@pytest.mark.parametrize(
    ("state", "expected"),
    (
        (CaptureOwnershipState(True, False, False), CaptureOwner.VIDEO),
        (CaptureOwnershipState(False, True, False), CaptureOwner.DIVE_TRACE),
        (CaptureOwnershipState(False, False, True), CaptureOwner.SLICE),
        (CaptureOwnershipState(False, False, False), None),
    ),
)
def test_capture_ownership_selects_the_single_lifecycle(state, expected):
    assert ViewerCaptureWorkflow.owner_for(state) is expected


@pytest.mark.parametrize(
    "active_owner",
    (None, CaptureOwner.VIDEO, CaptureOwner.DIVE_TRACE, CaptureOwner.SLICE),
)
@pytest.mark.parametrize(
    "requested_owner",
    (CaptureOwner.VIDEO, CaptureOwner.DIVE_TRACE, CaptureOwner.SLICE),
)
def test_capture_shortcuts_ignore_only_a_different_lifecycle_owner(
    active_owner,
    requested_owner,
):
    assert ViewerCaptureWorkflow.should_ignore_capture_shortcut(
        active_owner=active_owner,
        requested_owner=requested_owner,
    ) is (active_owner is not None and active_owner is not requested_owner)


@pytest.mark.parametrize(
    "owner",
    (None, CaptureOwner.VIDEO, CaptureOwner.DIVE_TRACE, CaptureOwner.SLICE),
)
def test_active_capture_has_no_persistent_instruction_banner(owner):
    assert (
        ViewerCaptureWorkflow.instruction_for(
            owner,
            primary_shortcut_label="Ctrl",
        )
        is None
    )
