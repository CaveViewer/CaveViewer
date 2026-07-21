"""Validate map-folder discovery for source models and compiled caches."""

from __future__ import annotations

from caveviewer.gui import map_selection


def test_missing_map_folder_is_rejected(tmp_path):
    valid, message = map_selection.validate_selected_map_folder(
        str(tmp_path / "missing")
    )

    assert not valid
    assert "not a valid folder" in message


def test_glb_map_folder_is_accepted(tmp_path):
    (tmp_path / "cave.glb").write_bytes(b"glb")

    assert map_selection.validate_selected_map_folder(str(tmp_path)) == (True, "")


def test_obj_with_declared_material_is_accepted(tmp_path):
    (tmp_path / "cave.obj").write_text("mtllib materials/cave.mtl\n", encoding="utf-8")
    material_dir = tmp_path / "materials"
    material_dir.mkdir()
    (material_dir / "cave.mtl").write_text("", encoding="utf-8")

    assert map_selection.validate_selected_map_folder(str(tmp_path)) == (True, "")


def test_obj_validation_uses_bounded_source_model_lookup(tmp_path, monkeypatch):
    obj = tmp_path / "cave.obj"
    material = tmp_path / "cave.mtl"
    obj.write_text("v 0 0 0\n", encoding="utf-8")
    material.write_text("", encoding="utf-8")
    calls = []

    def fake_lookup(obj_path):
        calls.append(obj_path)
        return str(material)

    monkeypatch.setattr(
        map_selection.source_model,
        "find_declared_material_file_for_obj",
        fake_lookup,
    )

    assert map_selection.validate_selected_map_folder(str(tmp_path)) == (True, "")
    assert calls == [str(obj)]


def test_obj_with_unsafe_material_path_is_rejected(tmp_path):
    (tmp_path / "cave.obj").write_text(
        "mtllib ../outside.mtl\nv 0 0 0\n",
        encoding="utf-8",
    )
    (tmp_path / "fallback.mtl").write_text("", encoding="utf-8")

    valid, message = map_selection.validate_selected_map_folder(str(tmp_path))

    assert not valid
    assert "Unsafe OBJ material path" in message


def test_obj_without_material_or_cache_is_rejected(tmp_path, monkeypatch):
    (tmp_path / "cave.obj").write_text("v 0 0 0\n", encoding="utf-8")
    monkeypatch.setattr(map_selection, "has_precompiled_cache", lambda _folder: False)

    valid, message = map_selection.validate_selected_map_folder(str(tmp_path))

    assert not valid
    assert "no matching .mtl file" in message
    assert "cache" not in message.lower()


def test_precompiled_cache_folder_is_accepted(tmp_path, monkeypatch):
    monkeypatch.setattr(map_selection, "has_precompiled_cache", lambda _folder: True)

    assert map_selection.validate_selected_map_folder(str(tmp_path)) == (True, "")
