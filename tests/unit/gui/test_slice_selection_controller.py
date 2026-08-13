"""Cover the countdown and anchor state used by Ctrl+C cave slicing."""

from __future__ import annotations

from caveviewer.gui.slice_selection_controller import (
    SliceSelectionController,
    SliceSelectionState,
)


def test_slice_selection_captures_start_only_after_shared_countdown():
    controller = SliceSelectionController()

    assert controller.start_countdown(now=10.0, start_number=3)
    assert controller.state is SliceSelectionState.COUNTDOWN
    assert controller.countdown_until == 14.0
    assert not controller.countdown_ready(now=13.99)
    assert controller.countdown_ready(now=14.0)
    assert controller.begin_selection((1.0, 2.0, 3.0))
    assert controller.state is SliceSelectionState.ACTIVE
    assert controller.start_anchor == (1.0, 2.0, 3.0)

    anchors = controller.finish_selection((4.0, 5.0, 6.0))

    assert anchors is not None
    assert anchors.start == (1.0, 2.0, 3.0)
    assert anchors.end == (4.0, 5.0, 6.0)
    assert controller.state is SliceSelectionState.SAVING
    controller.complete_export()
    assert controller.state is SliceSelectionState.IDLE


def test_slice_selection_second_shortcut_can_cancel_only_the_countdown():
    controller = SliceSelectionController()
    assert controller.start_countdown(now=0.0, start_number=3)

    assert controller.cancel_countdown()
    assert controller.state is SliceSelectionState.IDLE
    assert not controller.cancel_selection()
