"""Tests for validated, frame-rate-independent Recorded Dive playback."""

from __future__ import annotations

import json
import math

import numpy as np
import pytest

from caveviewer.gui.camera import FlyCamera
from caveviewer.gui.recorded_dive import (
    RecordedDiveFormatError,
    RecordedDiveMapError,
    RecordedDivePlaybackController,
    RecordedDivePlaybackState,
    has_recorded_dive_trace,
    load_recorded_dive_trace,
    resolve_recorded_dive_source_path,
    validate_recorded_dive_manifest,
)


def _pose_record(
    sample_index: int,
    elapsed_s: float,
    position: tuple[float, float, float],
    *,
    record: str = "sample",
    forward: tuple[float, float, float] = (1.0, 0.0, 0.0),
    up: tuple[float, float, float] = (0.0, 1.0, 0.0),
    right: tuple[float, float, float] = (0.0, 0.0, 1.0),
) -> dict:
    return {
        "record": record,
        "schema_version": 1,
        "session_id": "recorded-session",
        "sample_index": sample_index,
        "elapsed_s": elapsed_s,
        "position": list(position),
        "forward": list(forward),
        "up": list(up),
        "right": list(right),
        "yaw": math.atan2(forward[2], forward[0]),
        "pitch": math.asin(forward[1]),
        "roll": 0.0,
        "move_speed_m_per_second": 4.0,
    }


def _write_trace(tmp_path, poses: list[dict], *, completed: bool = True):
    trace_dir = tmp_path / "Map" / "_guided_dives"
    trace_dir.mkdir(parents=True)
    source = trace_dir.parent / "cave.obj"
    source.write_text("v 0 0 0\n", encoding="utf-8")
    records = [
        {
            "record": "trace_started",
            "schema_version": 1,
            "session_id": "recorded-session",
            "map": {
                "source_obj": "cave.obj",
                "manifest_version": 1,
                "chunk_size_m": 50.0,
                "triangle_count": 12,
                "coordinate_space": "manifest_xyz",
                "distance_unit": "meter",
                "orientation_unit": "radian",
            },
        },
        *poses,
    ]
    if completed:
        records.append(
            {
                "record": "trace_completed",
                "schema_version": 1,
                "session_id": "recorded-session",
                "duration_s": poses[-1]["elapsed_s"],
            }
        )
    path = trace_dir / "dive.jsonl"
    path.write_text(
        "".join(json.dumps(record) + "\n" for record in records),
        encoding="utf-8",
    )
    return path, source


def test_map_local_trace_detection_requires_a_jsonl_file(tmp_path):
    map_dir = tmp_path / "Map"
    map_dir.mkdir()

    assert not has_recorded_dive_trace(map_dir)

    trace_dir = map_dir / "_guided_dives"
    trace_dir.mkdir()
    (trace_dir / "notes.txt").write_text("not a trace", encoding="utf-8")
    assert not has_recorded_dive_trace(map_dir)

    (trace_dir / "dive.jsonl").write_text("{}\n", encoding="utf-8")
    assert has_recorded_dive_trace(map_dir)


def test_trace_interpolates_position_and_camera_basis(tmp_path):
    path, _source = _write_trace(
        tmp_path,
        [
            _pose_record(0, 0.0, (0.0, 0.0, 0.0)),
            _pose_record(
                1,
                2.0,
                (2.0, 2.0, 0.0),
                forward=(0.0, 0.0, 1.0),
                right=(-1.0, 0.0, 0.0),
            ),
        ],
    )

    trace = load_recorded_dive_trace(path)
    midpoint = trace.pose_at(1.0)

    assert midpoint.position == pytest.approx((1.0, 1.0, 0.0))
    assert midpoint.forward == pytest.approx(
        (math.sqrt(0.5), 0.0, math.sqrt(0.5))
    )
    assert np.dot(midpoint.forward, midpoint.right) == pytest.approx(0.0)
    assert trace.pose_at(2.0) is trace.final_pose


def test_declared_discontinuity_is_an_exact_jump_not_a_smoothed_segment(tmp_path):
    path, _source = _write_trace(
        tmp_path,
        [
            _pose_record(0, 0.0, (0.0, 0.0, 0.0)),
            _pose_record(
                1,
                1.0,
                (100.0, 0.0, 0.0),
                record="discontinuity",
            ),
            _pose_record(2, 2.0, (101.0, 0.0, 0.0)),
        ],
    )
    trace = load_recorded_dive_trace(path)

    assert trace.pose_at(0.999).position == (0.0, 0.0, 0.0)
    assert trace.pose_at(1.0).position == (100.0, 0.0, 0.0)
    assert trace.pose_at(1.5).position == pytest.approx((100.5, 0.0, 0.0))


def test_playback_clock_stops_for_chunk_loading_and_finishes_exactly(tmp_path):
    path, _source = _write_trace(
        tmp_path,
        [
            _pose_record(0, 0.0, (0.0, 0.0, 0.0)),
            _pose_record(1, 2.0, (2.0, 0.0, 0.0)),
        ],
    )
    trace = load_recorded_dive_trace(path)
    camera = FlyCamera()
    controller = RecordedDivePlaybackController(trace)
    controller.start(camera, now=10.0)

    controller.update(camera, now=15.0, chunks_ready=False)
    assert controller.state is RecordedDivePlaybackState.BUFFERING
    assert controller.elapsed_s == 0.0
    assert camera.position == pytest.approx([0.0, 0.0, 0.0])

    controller.update(camera, now=15.5, chunks_ready=True)
    assert controller.state is RecordedDivePlaybackState.PLAYING
    assert controller.elapsed_s == pytest.approx(0.5)
    assert camera.position == pytest.approx([0.5, 0.0, 0.0])

    controller.update(camera, now=17.5, chunks_ready=True)
    assert controller.state is RecordedDivePlaybackState.FINISHED
    assert controller.elapsed_s == 2.0
    assert camera.position == pytest.approx([2.0, 0.0, 0.0])


def test_pause_resume_does_not_add_paused_wall_time(tmp_path):
    path, _source = _write_trace(
        tmp_path,
        [
            _pose_record(0, 0.0, (0.0, 0.0, 0.0)),
            _pose_record(1, 5.0, (5.0, 0.0, 0.0)),
        ],
    )
    controller = RecordedDivePlaybackController(load_recorded_dive_trace(path))
    camera = FlyCamera()
    controller.start(camera, now=0.0)
    controller.update(camera, now=1.0, chunks_ready=True)
    assert controller.pause(now=1.0) is True

    controller.update(camera, now=20.0, chunks_ready=True)
    assert controller.elapsed_s == 1.0
    assert controller.resume(now=20.0) is True
    controller.update(camera, now=21.0, chunks_ready=True)

    assert controller.elapsed_s == 2.0
    assert camera.position == pytest.approx([2.0, 0.0, 0.0])


def test_loader_rejects_an_incomplete_trace(tmp_path):
    path, _source = _write_trace(
        tmp_path,
        [_pose_record(0, 0.0, (0.0, 0.0, 0.0))],
        completed=False,
    )

    with pytest.raises(RecordedDiveFormatError, match="trace_completed"):
        load_recorded_dive_trace(path)


def test_trace_resolves_adjacent_map_and_validates_cache_identity(tmp_path):
    path, source = _write_trace(
        tmp_path,
        [_pose_record(0, 0.0, (0.0, 0.0, 0.0))],
    )
    trace = load_recorded_dive_trace(path)
    manifest = {
        "version": 1,
        "source_obj": "cave.obj",
        "chunk_size": 50.0,
        "triangle_count": 12,
    }

    assert resolve_recorded_dive_source_path(trace) == source.resolve()
    validate_recorded_dive_manifest(trace, manifest)

    with pytest.raises(RecordedDiveMapError, match="geometry"):
        validate_recorded_dive_manifest(
            trace,
            {**manifest, "triangle_count": 13},
        )


def test_camera_accepts_recorded_basis_without_euler_round_trip():
    camera = FlyCamera()

    camera.set_orientation_basis(
        right=(-1.0, 0.0, 0.0),
        up=(0.0, 1.0, 0.0),
        forward=(0.0, 0.0, 1.0),
    )

    assert camera.right() == pytest.approx([-1.0, 0.0, 0.0])
    assert camera.up() == pytest.approx([0.0, 1.0, 0.0])
    assert camera.forward() == pytest.approx([0.0, 0.0, 1.0])
