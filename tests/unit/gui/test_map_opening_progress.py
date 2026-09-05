"""Tests for continuous map-opening presentation state."""

from __future__ import annotations

import pytest

from caveviewer.gui.map_opening_progress import MapOpeningProgressSession


def test_source_import_and_initial_streaming_share_one_monotonic_session():
    session = MapOpeningProgressSession()
    first_time_note = "First-time setup in progress. Next time, this map will open faster."

    import_frame = session.observe_import(
        "cave.glb",
        "writing chunk files",
        1.0,
        note=first_time_note,
    )
    later_import_frame = session.observe_import(
        "cave.glb",
        "building cache",
        1.0,
        note="A stage-specific note must not replace the task explanation.",
    )
    pausing_frame = session.observe_import(
        "cave.glb",
        "pausing import",
        1.0,
        note=first_time_note,
        supporting_note_override="Saving a resume point.",
    )
    restored_import_frame = session.observe_import(
        "cave.glb",
        "building cache",
        1.0,
        note=first_time_note,
    )
    first_streaming_frame = session.observe_streaming("cave.glb", 0.0)
    later_streaming_frame = session.observe_streaming("cave.glb", 0.5)
    complete_frame = session.complete("cave.glb")

    assert import_frame.session_id == first_streaming_frame.session_id
    assert import_frame.note == later_import_frame.note == first_time_note
    assert pausing_frame.note == "Saving a resume point."
    assert restored_import_frame.note == first_time_note
    assert first_streaming_frame.note == later_streaming_frame.note == first_time_note
    assert complete_frame.note == first_time_note
    assert import_frame.fraction == pytest.approx(0.90)
    assert first_streaming_frame.stage == "preparing cave"
    assert first_streaming_frame.fraction == pytest.approx(0.90)
    assert later_streaming_frame.fraction == pytest.approx(0.95)
    assert complete_frame.fraction == 1.0
    assert import_frame.title == first_streaming_frame.title == ""
    assert complete_frame.title == ""


def test_cached_map_uses_initial_streaming_measurement_directly():
    session = MapOpeningProgressSession()

    splash = session.begin_cached("cave.glb")
    streaming = session.observe_streaming("cave.glb", 0.25)

    assert splash.fraction is None
    assert splash.session_id == streaming.session_id
    assert streaming.fraction == pytest.approx(0.25)
    assert splash.note == streaming.note == ""


def test_import_progress_never_moves_backwards_when_worker_stages_regress():
    session = MapOpeningProgressSession()

    first = session.observe_import("cave.glb", "scanning file", 0.80, note="")
    second = session.observe_import("cave.glb", "writing chunk files", 0.20, note="")

    assert first.fraction == pytest.approx(0.72)
    assert second.fraction == pytest.approx(first.fraction)


def test_abandoned_or_completed_opening_creates_a_new_session_for_the_next_map():
    session = MapOpeningProgressSession()

    first = session.begin_import("first.glb")
    session.abandon()
    second = session.begin_import("first.glb")
    session.begin_streaming("first.glb")
    session.complete("first.glb")
    session.finish()
    third = session.begin_import("second.glb")

    assert second.session_id == first.session_id + 1
    assert third.session_id == second.session_id + 1
    assert second.fraction == 0.0
    assert third.fraction == 0.0
