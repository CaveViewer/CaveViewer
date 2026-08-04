"""Tests for asynchronous manual Guided Dive reference tracing."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path

import pytest

from caveviewer.core.map.cache_identity import GUIDED_DIVE_CACHE_IDENTITY_KEY
from caveviewer.gui import manual_dive_trace
from caveviewer.gui.manual_dive_trace import (
    ManualDivePose,
    ManualDiveTraceRecorder,
    manual_dive_trace_directory,
    manual_dive_trace_map_context,
)


def _pose(
    x: float = 0.0,
    y: float = 0.0,
    z: float = 0.0,
    *,
    forward: tuple[float, float, float] = (1.0, 0.0, 0.0),
) -> ManualDivePose:
    return ManualDivePose(
        position=(x, y, z),
        forward=forward,
        up=(0.0, 1.0, 0.0),
        right=(0.0, 0.0, 1.0),
        yaw=0.0,
        pitch=0.0,
        roll=0.0,
        move_speed_m_per_second=4.0,
    )


def _records(path) -> list[dict]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
    ]


def _cache_identity() -> dict[str, int | str]:
    return {
        "version": 1,
        "source_sha256": "a" * 64,
        "cache_manifest_sha256": "b" * 64,
    }


def _trace_map_context(*, source_obj: str = "cave.obj") -> dict:
    return {
        "source_obj": source_obj,
        "cache_identity": _cache_identity(),
    }


def _recorder(tmp_path, **kwargs) -> ManualDiveTraceRecorder:
    map_context = kwargs.pop("map_context", _trace_map_context())
    return ManualDiveTraceRecorder(
        tmp_path / "_guided_dives",
        map_context=map_context,
        utc_now=lambda: datetime(2026, 8, 2, 12, 30, tzinfo=timezone.utc),
        **kwargs,
    )


def test_trace_directory_is_map_local_independent_of_cache_location(tmp_path):
    map_root = tmp_path / "Map"

    assert manual_dive_trace_directory(map_root) == map_root / "_guided_dives"


def test_trace_directory_requires_a_map_root():
    with pytest.raises(ValueError, match="map root"):
        manual_dive_trace_directory("")


def test_map_context_keeps_source_basename_and_render_identity_only():
    assert manual_dive_trace_map_context(
        {
            "version": 1,
            "source_obj": "/private/maps/cave.obj",
            "chunk_size": 50.0,
            GUIDED_DIVE_CACHE_IDENTITY_KEY: _cache_identity(),
            "navigation": {
                "version": 12,
                "method": "fixed_orthogonal_voxel_graph_v12",
                "navigation_start_anchor": {
                    "position": [1.0, 2.0, 3.0],
                    "source_vertex_index": 0,
                },
            },
        }
    ) == {
        "source_obj": "cave.obj",
        "manifest_version": 1,
        "chunk_size_m": 50.0,
        "triangle_count": None,
        "coordinate_space": "manifest_xyz",
        "distance_unit": "meter",
        "orientation_unit": "radian",
        "cache_identity": _cache_identity(),
    }


def test_manual_trace_writes_start_samples_and_completion(tmp_path):
    recorder = _recorder(
        tmp_path,
        map_context=_trace_map_context(source_obj="small.obj"),
        sample_interval_s=0.10,
        sample_distance_m=0.25,
    )
    output_path = recorder.start(_pose(), now=10.0)

    assert recorder.observe(_pose(0.05), now=10.05) is False
    assert recorder.observe(_pose(0.10), now=10.11) is True
    assert recorder.observe(_pose(0.40), now=10.12) is True
    recorder.stop(_pose(0.50), now=10.20)

    result = recorder.wait()
    assert result is not None
    assert result.completed is True
    assert result.output_path == output_path
    records = _records(tmp_path / "_guided_dives" / Path(output_path).name)
    assert [record["record"] for record in records] == [
        "trace_started",
        "sample",
        "sample",
        "sample",
        "sample",
        "trace_completed",
    ]
    assert records[0]["map"]["source_obj"] == "small.obj"
    assert records[0]["schema_version"] == 2
    assert records[0]["map"]["cache_identity"] == _cache_identity()
    assert records[-1]["reason"] == "user_stopped"
    assert records[-1]["total_flown_distance_m"] == 0.5
    assert records[-1]["dropped_sample_count"] == 0
    assert records[-1]["bounds"] == {
        "minimum": [0.0, 0.0, 0.0],
        "maximum": [0.5, 0.0, 0.0],
    }


def test_trace_closes_partial_file_before_atomic_publish(tmp_path, monkeypatch):
    recorder = _recorder(tmp_path)
    opened_partial_files = []
    real_open = Path.open
    real_replace = manual_dive_trace.os.replace

    def track_open(path, *args, **kwargs):
        file_obj = real_open(path, *args, **kwargs)
        mode = args[0] if args else kwargs.get("mode", "r")
        if mode == "x":
            opened_partial_files.append(file_obj)
        return file_obj

    def replace_after_close(source, destination):
        assert opened_partial_files
        assert all(file_obj.closed for file_obj in opened_partial_files)
        return real_replace(source, destination)

    monkeypatch.setattr(Path, "open", track_open)
    monkeypatch.setattr(manual_dive_trace.os, "replace", replace_after_close)

    recorder.start(_pose(), now=0.0)
    recorder.stop(_pose(), now=0.1)

    result = recorder.wait()
    assert result is not None
    assert result.completed is True


def test_trace_requires_a_stable_cache_identity(tmp_path):
    recorder = ManualDiveTraceRecorder(
        tmp_path / "_guided_dives",
        map_context={"source_obj": "cave.obj"},
    )

    with pytest.raises(ValueError, match="stable identity"):
        recorder.start(_pose(), now=0.0)


def test_orientation_threshold_and_stationary_heartbeat_are_recorded(tmp_path):
    recorder = _recorder(
        tmp_path,
        sample_interval_s=10.0,
        sample_distance_m=10.0,
        sample_angle_deg=2.0,
        heartbeat_interval_s=1.0,
    )
    recorder.start(_pose(), now=0.0)

    assert recorder.observe(_pose(), now=0.5) is False
    assert recorder.observe(
        _pose(forward=(0.0, 0.0, 1.0)),
        now=0.6,
    ) is True
    assert recorder.observe(
        _pose(forward=(0.0, 0.0, 1.0)),
        now=1.7,
    ) is True
    recorder.stop(_pose(forward=(0.0, 0.0, 1.0)), now=2.0)
    assert recorder.wait().completed is True


def test_discontinuity_does_not_count_as_flown_distance(tmp_path):
    recorder = _recorder(tmp_path)
    recorder.start(_pose(), now=0.0)
    recorder.observe(_pose(1.0), now=0.2)
    recorder.mark_discontinuity(
        _pose(1.0),
        _pose(101.0),
        reason="bookmark_recall",
        now=0.3,
    )
    recorder.observe(_pose(102.0), now=0.5)
    recorder.stop(_pose(102.0), now=0.6)

    result = recorder.wait()
    records = _records(
        tmp_path / "_guided_dives" / Path(result.output_path).name
    )
    completed = records[-1]
    assert completed["total_flown_distance_m"] == 2.0
    assert completed["teleport_distance_m"] == 100.0
    assert completed["discontinuity_count"] == 1
    discontinuity = next(
        record for record in records if record["record"] == "discontinuity"
    )
    assert discontinuity["reason"] == "bookmark_recall"


def test_trace_sample_cap_drops_regular_samples_but_keeps_final_pose(tmp_path):
    recorder = _recorder(
        tmp_path,
        sample_interval_s=0.0,
        sample_distance_m=0.0,
        sample_cap=3,
    )
    recorder.start(_pose(), now=0.0)
    recorder.observe(_pose(1.0), now=0.1)
    recorder.observe(_pose(2.0), now=0.2)
    assert recorder.observe(_pose(3.0), now=0.3) is False
    recorder.stop(_pose(4.0), now=0.4)

    result = recorder.wait()
    records = _records(
        tmp_path / "_guided_dives" / Path(result.output_path).name
    )
    assert records[-1]["sample_cap_reached"] is True
    assert records[-1]["dropped_sample_count"] >= 1
    assert records[-1]["final_pose"]["position"] == [4.0, 0.0, 0.0]


def test_background_write_failure_is_nonfatal_and_reported(tmp_path):
    not_a_directory = tmp_path / "trace-file"
    not_a_directory.write_text("occupied", encoding="utf-8")
    recorder = ManualDiveTraceRecorder(
        not_a_directory,
        map_context=_trace_map_context(),
        utc_now=lambda: datetime(2026, 8, 2, tzinfo=timezone.utc),
    )

    recorder.start(_pose(), now=0.0)
    recorder.stop(_pose(), now=0.1)

    result = recorder.wait()
    assert result is not None
    assert result.completed is False
    assert result.error is not None
