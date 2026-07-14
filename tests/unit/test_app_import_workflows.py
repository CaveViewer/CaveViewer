"""Tests for application-level model import and cache orchestration."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from caveviewer import app
from caveviewer.core import chunker, glb_parser, obj_parser
from caveviewer.storage_paths import resolve_application_paths


def _mesh(*material_names):
    ranges = [
        obj_parser.MaterialRange(name, index, index + 1)
        for index, name in enumerate(material_names)
    ]
    return SimpleNamespace(face_pos_idx=[object()] * max(1, len(ranges)), material_ranges=ranges)


def test_obj_import_reuses_a_valid_cache(monkeypatch):
    monkeypatch.setattr(chunker, "cache_is_valid", lambda _path: True)
    monkeypatch.setattr(chunker, "get_cache_dir", lambda _path: "/cache/reused")
    monkeypatch.setattr(
        obj_parser,
        "parse_obj",
        lambda *_args, **_kwargs: pytest.fail("valid caches must skip parsing"),
    )

    assert app.import_and_cache("map.obj", "map.mtl") == "/cache/reused"


def test_obj_import_builds_cache_reports_progress_and_stages_only_existing_textures(
    tmp_path, monkeypatch
):
    source = tmp_path / "map.obj"
    source.write_text("mesh", encoding="utf-8")
    material_file = tmp_path / "map.mtl"
    material_file.write_text("materials", encoding="utf-8")
    (tmp_path / "copy.jpg").write_bytes(b"copy")
    (tmp_path / "existing.jpg").write_bytes(b"source-existing")
    cache_dir = tmp_path / "managed-cache"
    cache_dir.mkdir()
    progress = []
    build_options = {}
    mesh = _mesh("copy", "missing", "existing", "plain")
    materials = {
        "copy": SimpleNamespace(diffuse_texture="copy.jpg"),
        "missing": SimpleNamespace(diffuse_texture="missing.jpg"),
        "existing": SimpleNamespace(diffuse_texture="existing.jpg"),
        "plain": SimpleNamespace(diffuse_texture=None),
    }

    monkeypatch.setattr(chunker, "cache_is_valid", lambda _path: False)
    monkeypatch.setattr(
        chunker, "ensure_sufficient_disk_space", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        chunker, "ensure_sufficient_import_memory", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(chunker, "configured_chunk_size", lambda: 8.0)

    def parse_obj(_path, progress_cb, **_kwargs):
        progress_cb("parse-low", -1.0)
        progress_cb("parse-high", 2.0)
        return mesh

    def build_cache(
        _path, received_mesh, received_materials, progress_cb, **options
    ):
        assert received_mesh is mesh
        assert received_materials is materials
        build_options.update(options)
        progress_cb("cache-low", -1.0)
        progress_cb("cache-high", 2.0)
        return str(cache_dir)

    monkeypatch.setattr(obj_parser, "parse_obj", parse_obj)
    monkeypatch.setattr(obj_parser, "parse_mtl", lambda _path: materials)
    monkeypatch.setattr(chunker, "build_cache", build_cache)
    monkeypatch.setattr(
        chunker, "load_manifest", lambda _path: {"chunks": {"0,0": {}, "1,0": {}}}
    )
    times = iter((10.0, 12.5))
    monkeypatch.setattr(app.time, "time", lambda: next(times))

    result = app.import_and_cache(
        str(source), str(material_file), extra_progress_cb=lambda *item: progress.append(item)
    )

    assert result == str(cache_dir)
    assets = build_options["assets"]
    assert [(asset.relative_path, asset.source_path) for asset in assets] == [
        ("copy.jpg", str(tmp_path / "copy.jpg")),
        ("existing.jpg", str(tmp_path / "existing.jpg")),
    ]
    assert build_options["cache_dir"].startswith(
        str(resolve_application_paths().cache_dir / "maps")
    )
    assert progress == [
        ("parse-low", 0.0),
        ("parse-high", 1.0),
        ("cache-low", 0.0),
        ("cache-high", 1.0),
    ]


@pytest.mark.parametrize("size_behavior", ["large", "error"])
def test_obj_import_handles_large_or_unreadable_source_size(
    tmp_path, monkeypatch, size_behavior
):
    source = tmp_path / "map.obj"
    source.write_bytes(b"mesh")
    material_file = tmp_path / "map.mtl"
    material_file.write_text("material", encoding="utf-8")
    cache_dir = tmp_path / "managed-cache"
    cache_dir.mkdir()
    mesh = _mesh()

    monkeypatch.setattr(chunker, "cache_is_valid", lambda _path: False)
    monkeypatch.setattr(
        chunker, "ensure_sufficient_disk_space", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        chunker, "ensure_sufficient_import_memory", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(chunker, "configured_chunk_size", lambda: 8.0)
    monkeypatch.setattr(obj_parser, "parse_obj", lambda *_args, **_kwargs: mesh)
    monkeypatch.setattr(obj_parser, "parse_mtl", lambda _path: {})
    monkeypatch.setattr(chunker, "build_cache", lambda *_args, **_kwargs: str(cache_dir))
    monkeypatch.setattr(chunker, "load_manifest", lambda _path: {"chunks": {}})
    if size_behavior == "large":
        monkeypatch.setattr(app.os.path, "getsize", lambda _path: 11 * 1024**3)
    else:
        monkeypatch.setattr(
            app.os.path, "getsize", lambda _path: (_ for _ in ()).throw(OSError())
        )

    assert app.import_and_cache(str(source), str(material_file), force_rebuild=True) == str(
        cache_dir
    )


def test_format_agnostic_obj_import_delegates_all_options(monkeypatch):
    received = []
    callback = object()
    monkeypatch.setattr(
        app,
        "import_and_cache",
        lambda *args, **kwargs: received.append((args, kwargs)) or "/cache/obj",
    )

    result = app.import_and_cache_any(
        {"format": "obj", "obj_path": "map.obj", "mtl_path": "map.mtl"},
        "textures",
        force_rebuild=True,
        extra_progress_cb=callback,
    )

    assert result == "/cache/obj"
    assert received == [
        (
            ("map.obj", "map.mtl"),
            {"force_rebuild": True, "extra_progress_cb": callback},
        )
    ]


def test_glb_import_reuses_a_valid_cache(monkeypatch):
    monkeypatch.setattr(chunker, "cache_is_valid", lambda _path: True)
    monkeypatch.setattr(chunker, "get_cache_dir", lambda _path: "/cache/glb")
    monkeypatch.setattr(
        glb_parser,
        "parse_glb",
        lambda *_args, **_kwargs: pytest.fail("valid caches must skip parsing"),
    )

    assert (
        app.import_and_cache_any(
            {"format": "glb", "glb_path": "map.glb"}, "textures"
        )
        == "/cache/glb"
    )


def test_glb_import_stages_embedded_texture_without_writing_source_directory(
    tmp_path, monkeypatch
):
    source = tmp_path / "map.glb"
    source.write_bytes(b"glTF")
    textures_dir = tmp_path / "textures"
    cache_dir = tmp_path / "managed-cache"
    cache_dir.mkdir()
    mesh = _mesh("embedded", "plain")
    progress = []
    build_options = {}

    monkeypatch.setattr(chunker, "cache_is_valid", lambda _path: False)
    monkeypatch.setattr(
        chunker, "ensure_sufficient_disk_space", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        chunker, "ensure_sufficient_import_memory", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(chunker, "configured_chunk_size", lambda: 8.0)
    monkeypatch.setattr(app.os.path, "getsize", lambda _path: 11 * 1024**3)

    def parse_glb(_path, progress_cb):
        progress_cb("parse", 0.5)
        return mesh, {"embedded": b"\x89PNG\r\n\x1a\npixels"}

    def build_cache(_path, received_mesh, materials, progress_cb, **options):
        assert received_mesh is mesh
        assert materials["embedded"].diffuse_texture == "embedded.png"
        assert materials["plain"].diffuse_texture is None
        build_options.update(options)
        progress_cb("cache", 0.5)
        return str(cache_dir)

    monkeypatch.setattr(glb_parser, "parse_glb", parse_glb)
    monkeypatch.setattr(chunker, "build_cache", build_cache)
    monkeypatch.setattr(chunker, "load_manifest", lambda _path: {"chunks": {"0,0": {}}})

    result = app.import_and_cache_any(
        {"format": "glb", "glb_path": str(source)},
        str(textures_dir),
        extra_progress_cb=lambda *item: progress.append(item),
    )

    assert result == str(cache_dir)
    assert not textures_dir.exists()
    assert [asset.relative_path for asset in build_options["assets"]] == [
        "embedded.png"
    ]
    assert build_options["assets"][0].data == b"\x89PNG\r\n\x1a\npixels"
    assert progress == [("parse", 0.25), ("cache", 0.75)]


def test_glb_import_rejects_unknown_model_format_after_capacity_check(monkeypatch):
    monkeypatch.setattr(chunker, "cache_is_valid", lambda _path: False)
    monkeypatch.setattr(
        chunker, "ensure_sufficient_disk_space", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(chunker, "configured_chunk_size", lambda: 8.0)
    monkeypatch.setattr(app.os.path, "getsize", lambda _path: 0)

    with pytest.raises(ValueError, match="Unknown model format"):
        app.import_and_cache_any(
            {"format": "unsupported", "glb_path": "map.data"}, "textures"
        )
