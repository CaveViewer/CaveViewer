"""Test action-time map-library Guided Dive capability preflight."""

from __future__ import annotations

import json

from caveviewer.core.capabilities import CapabilityStatus
from caveviewer.gui import guided_dive_playback
from caveviewer.gui.features import FeatureState
from caveviewer.gui.manual_dive_trace import MANUAL_DIVE_TRACE_SCHEMA_VERSION


def _cache_identity(*, source_sha256: str = "a" * 64) -> dict[str, object]:
    return {
        "version": 1,
        "source_sha256": source_sha256,
        "cache_manifest_sha256": "b" * 64,
    }


def _manifest(*, source_sha256: str = "a" * 64) -> dict[str, object]:
    return {
        "version": 1,
        "source_obj": "cave.obj",
        "chunk_size": 50.0,
        "triangle_count": 12,
        "guided_dive_identity": _cache_identity(source_sha256=source_sha256),
    }


def _write_trace(map_root, *, name: str = "favorite.jsonl"):
    source = map_root / "cave.obj"
    source.write_text("v 0 0 0\n", encoding="utf-8")
    trace_directory = map_root / "_guided_dives"
    trace_directory.mkdir()
    trace_path = trace_directory / name
    records = [
        {
            "record": "trace_started",
            "schema_version": MANUAL_DIVE_TRACE_SCHEMA_VERSION,
            "session_id": "guided-dive",
            "map": {
                "source_obj": "cave.obj",
                "manifest_version": 1,
                "chunk_size_m": 50.0,
                "triangle_count": 12,
                "cache_identity": _cache_identity(),
                "coordinate_space": "manifest_xyz",
                "distance_unit": "meter",
                "orientation_unit": "radian",
            },
        },
        {
            "record": "sample",
            "schema_version": MANUAL_DIVE_TRACE_SCHEMA_VERSION,
            "session_id": "guided-dive",
            "sample_index": 0,
            "elapsed_s": 0.0,
            "position": [1.0, 2.0, 3.0],
            "forward": [1.0, 0.0, 0.0],
            "up": [0.0, 1.0, 0.0],
            "right": [0.0, 0.0, 1.0],
            "yaw": 0.0,
            "pitch": 0.0,
            "roll": 0.0,
            "move_speed_m_per_second": 4.0,
        },
        {
            "record": "trace_completed",
            "schema_version": MANUAL_DIVE_TRACE_SCHEMA_VERSION,
            "session_id": "guided-dive",
            "duration_s": 0.0,
        },
    ]
    trace_path.write_text(
        "".join(json.dumps(record) + "\n" for record in records),
        encoding="utf-8",
    )
    return source, trace_path


def test_trace_directory_capability_hides_the_menu_without_jsonl(tmp_path):
    map_root = tmp_path / "Cave"
    map_root.mkdir()

    missing = guided_dive_playback.probe_guided_dive_trace_directory(map_root)

    assert missing.status is CapabilityStatus.UNAVAILABLE
    assert missing.reason_code == "guided_dive_trace_unavailable"
    assert (
        guided_dive_playback.guided_dive_menu_decision(map_root).state
        is FeatureState.HIDDEN
    )

    trace_directory = map_root / "_guided_dives"
    trace_directory.mkdir()
    (trace_directory / "notes.txt").write_text("not a trace", encoding="utf-8")

    empty = guided_dive_playback.probe_guided_dive_trace_directory(map_root)

    assert empty.status is CapabilityStatus.UNAVAILABLE


def test_playback_preflight_accepts_a_map_local_matching_cache(tmp_path, monkeypatch):
    map_root = tmp_path / "Cave"
    map_root.mkdir()
    source, trace_path = _write_trace(map_root)
    cache_dir = map_root / "_cache"
    cache_dir.mkdir()
    monkeypatch.setattr(
        guided_dive_playback.chunker,
        "cache_is_valid",
        lambda path: path == str(source.resolve()),
    )
    monkeypatch.setattr(
        guided_dive_playback.chunker,
        "get_cache_dir",
        lambda _path: str(cache_dir),
    )
    monkeypatch.setattr(
        guided_dive_playback.chunker,
        "load_manifest",
        lambda _path: _manifest(),
    )

    preflight = guided_dive_playback.guided_dive_playback_preflight(
        map_root,
        trace_path,
    )

    assert preflight.capability.status is CapabilityStatus.AVAILABLE
    assert preflight.decision.state is FeatureState.ENABLED
    assert preflight.capability.value is not None
    assert preflight.capability.value.trace.path == trace_path.resolve()
    assert preflight.capability.value.source_path == source.resolve()
    assert preflight.capability.value.cache_dir == cache_dir.resolve()


def test_playback_preflight_rejects_an_invalid_selected_trace(tmp_path):
    map_root = tmp_path / "Cave"
    map_root.mkdir()
    trace_directory = map_root / "_guided_dives"
    trace_directory.mkdir()
    trace_path = trace_directory / "broken.jsonl"
    trace_path.write_text("{}\n", encoding="utf-8")

    preflight = guided_dive_playback.guided_dive_playback_preflight(
        map_root,
        trace_path,
    )

    assert preflight.capability.status is CapabilityStatus.UNAVAILABLE
    assert preflight.capability.reason_code == "guided_dive_trace_invalid"
    assert preflight.decision.state is FeatureState.DISABLED


def test_playback_preflight_rejects_a_stale_or_missing_cache(tmp_path, monkeypatch):
    map_root = tmp_path / "Cave"
    map_root.mkdir()
    _source, trace_path = _write_trace(map_root)
    monkeypatch.setattr(
        guided_dive_playback.chunker,
        "cache_is_valid",
        lambda _path: False,
    )

    preflight = guided_dive_playback.guided_dive_playback_preflight(
        map_root,
        trace_path,
    )

    assert preflight.capability.status is CapabilityStatus.UNAVAILABLE
    assert preflight.capability.reason_code == "guided_dive_cache_unavailable"
    assert preflight.decision.state is FeatureState.DISABLED


def test_playback_preflight_rejects_a_different_cache_identity(tmp_path, monkeypatch):
    map_root = tmp_path / "Cave"
    map_root.mkdir()
    source, trace_path = _write_trace(map_root)
    cache_dir = map_root / "_cache"
    cache_dir.mkdir()
    monkeypatch.setattr(
        guided_dive_playback.chunker,
        "cache_is_valid",
        lambda path: path == str(source.resolve()),
    )
    monkeypatch.setattr(
        guided_dive_playback.chunker,
        "get_cache_dir",
        lambda _path: str(cache_dir),
    )
    monkeypatch.setattr(
        guided_dive_playback.chunker,
        "load_manifest",
        lambda _path: _manifest(source_sha256="c" * 64),
    )

    preflight = guided_dive_playback.guided_dive_playback_preflight(
        map_root,
        trace_path,
    )

    assert preflight.capability.status is CapabilityStatus.UNAVAILABLE
    assert preflight.capability.reason_code == "guided_dive_cache_incompatible"
    assert preflight.decision.state is FeatureState.DISABLED


def test_playback_preflight_rejects_a_trace_outside_the_selected_map(tmp_path):
    map_root = tmp_path / "Cave"
    map_root.mkdir()
    trace_directory = map_root / "_guided_dives"
    trace_directory.mkdir()
    (trace_directory / "present.jsonl").write_text("{}\n", encoding="utf-8")
    external_trace = tmp_path / "other.jsonl"
    external_trace.write_text("{}\n", encoding="utf-8")

    preflight = guided_dive_playback.guided_dive_playback_preflight(
        map_root,
        external_trace,
    )

    assert preflight.capability.status is CapabilityStatus.UNAVAILABLE
    assert preflight.capability.reason_code == "guided_dive_trace_not_map_local"
    assert preflight.decision.state is FeatureState.DISABLED
