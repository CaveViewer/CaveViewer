"""Tests for splash map-library display models."""

from __future__ import annotations

import json

from caveviewer.gui import map_library


def test_recent_map_entry_uses_folder_name_without_directory_detail(tmp_path):
    parent = tmp_path / "a" / "long" / "path"
    map_root = parent / "Demo Map"
    map_root.mkdir(parents=True)

    entry = map_library.recent_map_entry(str(map_root))

    assert entry.path == str(map_root)
    assert entry.key == map_library.recent_map_key(str(map_root))
    assert entry.title == "Demo Map"
    assert entry.detail == ""


def test_recent_map_title_uses_source_model_name(tmp_path):
    map_root = tmp_path / "Folder Name"
    map_root.mkdir()
    (map_root / "DevilsEyeGoldLine_resized.glb").write_bytes(b"")

    assert (
        map_library.recent_map_title(str(map_root))
        == "DevilsEyeGoldLine_resized"
    )


def test_recent_map_title_recovers_stale_cache_history_entry(tmp_path):
    cache_dir = tmp_path / "DevilsEyeGoldLine_resized-f566598453a9e673"
    cache_dir.mkdir()
    (cache_dir / "manifest.json").write_text(
        '{"version": 2, "source_obj": "DevilsEyeGoldLine_resized.glb", "chunks": {}}',
        encoding="utf-8",
    )

    assert (
        map_library.recent_map_title(str(cache_dir))
        == "DevilsEyeGoldLine_resized"
    )


def test_recent_slice_entry_uses_root_cave_name_only_for_metadata_lookup(tmp_path):
    map_root = tmp_path / "Ginnie Springs - Segment 2"
    map_root.mkdir()
    marker = map_root / "Ginnie Springs - Segment 2.cvslice"
    marker.write_text(
        json.dumps(
            {
                "format": "caveviewer.slice",
                "schema_version": 1,
                "display_name": "Ginnie Springs - Segment 2",
                "root_cave_name": "Ginnie Springs",
            }
        ),
        encoding="utf-8",
    )
    (map_root / "manifest.json").write_text(
        json.dumps({"source_obj": marker.name, "chunks": {}}),
        encoding="utf-8",
    )

    entry = map_library.recent_map_entry(str(map_root))

    assert entry.title == "Ginnie Springs - Segment 2"
    assert entry.cave_lookup_title == "Ginnie Springs"


def test_recent_slice_entry_ignores_malformed_marker_metadata(tmp_path):
    map_root = tmp_path / "Broken Slice"
    map_root.mkdir()
    marker = map_root / "Broken Slice.cvslice"
    marker.write_text("{broken", encoding="utf-8")
    (map_root / "manifest.json").write_text(
        json.dumps({"source_obj": marker.name, "chunks": {}}),
        encoding="utf-8",
    )

    entry = map_library.recent_map_entry(str(map_root))

    assert entry.title == "Broken Slice"
    assert entry.cave_lookup_title is None
