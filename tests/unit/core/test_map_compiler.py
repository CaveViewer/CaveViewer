"""Cover non-GUI map compilation orchestration."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from caveviewer.core.map import compiler as map_compiler
from caveviewer.core.map.cache_paths import MapCacheLocator
from caveviewer.core.map.chunk_size_advisor import ChunkSizeRecommendation


def _write_valid_cache(
    source: Path,
    cache_root: Path,
    *,
    chunk_size: float,
    max_upload_group_mb: float | None = None,
) -> Path:
    cache_dir = MapCacheLocator(
        environ={"CAVEVIEWER_MAP_CACHE_DIR": str(cache_root)}
    ).build_cache_dir(source)
    chunks_dir = cache_dir / "chunks"
    chunks_dir.mkdir(parents=True)
    manifest_path = cache_dir / "manifest.json"
    manifest = {
        "version": 1,
        "chunk_size": chunk_size,
        "chunks": {"0_0_0": {}},
        "triangle_count": 12,
    }
    if max_upload_group_mb is not None:
        manifest["max_upload_group_mb"] = max_upload_group_mb
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    newer_than_source = source.stat().st_mtime + 1
    os.utime(manifest_path, (newer_than_source, newer_than_source))
    return cache_dir


def test_compile_skips_valid_cache_with_matching_chunk_size(
    tmp_path, monkeypatch
):
    source = tmp_path / "cave.glb"
    source.write_bytes(b"glTF")
    cache_root = tmp_path / "cache-root"
    cache_dir = _write_valid_cache(source, cache_root, chunk_size=64.0)
    monkeypatch.delenv("CAVEVIEWER_MAP_CACHE_DIR", raising=False)

    monkeypatch.setattr(
        map_compiler,
        "import_and_cache_any",
        lambda *_args, **_kwargs: pytest.fail("matching cache must not rebuild"),
    )

    result = map_compiler.compile_map(
        map_compiler.CompileOptions(
            source=str(source),
            cache_root=str(cache_root),
            parsing_overrides={"chunk_size_meters": "64"},
        )
    )

    assert result.status == "skipped"
    assert result.cache_dir == str(cache_dir)
    assert result.cache_root == str(cache_root)
    assert result.chunk_count == 1
    assert result.triangle_count == 12
    assert result.chunk_size == 64.0
    assert "CAVEVIEWER_MAP_CACHE_DIR" not in os.environ


def test_compile_rebuilds_valid_cache_when_chunk_size_differs(
    tmp_path, monkeypatch
):
    source = tmp_path / "cave.glb"
    source.write_bytes(b"glTF")
    cache_root = tmp_path / "cache-root"
    cache_dir = _write_valid_cache(source, cache_root, chunk_size=50.0)
    monkeypatch.delenv("CAVEVIEWER_MAP_CACHE_DIR", raising=False)
    monkeypatch.delenv("CAVEVIEWER_MAX_UPLOAD_GROUP_MB", raising=False)
    monkeypatch.delenv("CAVEVIEWER_OBJ_IMPORT_BATCH_FACES", raising=False)
    monkeypatch.delenv("CAVEVIEWER_OBJ_BUCKET_WORKERS", raising=False)

    calls = []

    def fake_import(model_descriptor, textures_dir, **kwargs):
        calls.append((model_descriptor, textures_dir, kwargs, os.environ.copy()))
        (cache_dir / "manifest.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "chunk_size": 64.0,
                    "chunks": {"0_0_0": {}, "1_0_0": {}},
                }
            ),
            encoding="utf-8",
        )
        return str(cache_dir)

    elapsed_markers = iter([100.0, 102.5])
    monkeypatch.setattr(
        map_compiler.time,
        "perf_counter",
        lambda: next(elapsed_markers),
    )
    monkeypatch.setattr(map_compiler, "import_and_cache_any", fake_import)

    result = map_compiler.compile_map(
        map_compiler.CompileOptions(
            source=str(source),
            cache_root=str(cache_root),
            parsing_overrides={
                "chunk_size_meters": "64",
                "max_upload_group_mb": "24",
                "obj_import_batch_thousands": "250",
            },
            obj_bucket_workers="4",
            json_output=True,
        )
    )

    assert result.status == "built"
    assert result.elapsed_seconds == 2.5
    assert result.rebuilt_for_chunk_size is True
    assert result.chunk_count == 2
    _descriptor, textures_dir, kwargs, environ = calls[0]
    assert textures_dir == str(tmp_path)
    assert kwargs["force_rebuild"] is True
    assert kwargs["progress_cb"] is None
    assert kwargs["chunk_size"] == 64.0
    assert kwargs["cache_dir"] == str(cache_dir)
    assert kwargs["max_upload_group_mb"] == 24.0
    assert kwargs["obj_import_batch_faces"] == 250000
    assert kwargs["obj_bucket_workers"] == 4
    assert "CAVEVIEWER_MAP_CACHE_DIR" not in os.environ
    assert "CAVEVIEWER_MAX_UPLOAD_GROUP_MB" not in environ
    assert "CAVEVIEWER_OBJ_IMPORT_BATCH_FACES" not in environ
    assert "CAVEVIEWER_OBJ_BUCKET_WORKERS" not in environ


def test_compile_rebuilds_valid_cache_when_upload_group_size_differs(
    tmp_path, monkeypatch
):
    source = tmp_path / "cave.glb"
    source.write_bytes(b"glTF")
    cache_root = tmp_path / "cache-root"
    cache_dir = _write_valid_cache(
        source,
        cache_root,
        chunk_size=64.0,
        max_upload_group_mb=32.0,
    )
    monkeypatch.delenv("CAVEVIEWER_MAP_CACHE_DIR", raising=False)
    monkeypatch.delenv("CAVEVIEWER_MAX_UPLOAD_GROUP_MB", raising=False)

    calls = []

    def fake_import(model_descriptor, textures_dir, **kwargs):
        calls.append((model_descriptor, textures_dir, kwargs, os.environ.copy()))
        (cache_dir / "manifest.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "chunk_size": 64.0,
                    "max_upload_group_mb": 16.0,
                    "chunks": {"0_0_0": {}, "1_0_0": {}},
                }
            ),
            encoding="utf-8",
        )
        return str(cache_dir)

    elapsed_markers = iter([100.0, 101.0])
    monkeypatch.setattr(
        map_compiler.time,
        "perf_counter",
        lambda: next(elapsed_markers),
    )
    monkeypatch.setattr(map_compiler, "import_and_cache_any", fake_import)

    result = map_compiler.compile_map(
        map_compiler.CompileOptions(
            source=str(source),
            cache_root=str(cache_root),
            parsing_overrides={
                "chunk_size_meters": "64",
                "max_upload_group_mb": "16",
            },
        )
    )

    assert result.status == "built"
    assert result.rebuilt_for_chunk_size is False
    assert result.chunk_count == 2
    _descriptor, _textures_dir, kwargs, environ = calls[0]
    assert kwargs["force_rebuild"] is True
    assert kwargs["chunk_size"] == 64.0
    assert kwargs["cache_dir"] == str(cache_dir)
    assert kwargs["max_upload_group_mb"] == 16.0
    assert "CAVEVIEWER_MAX_UPLOAD_GROUP_MB" not in environ


def test_dry_run_reports_planned_cache_without_importing(tmp_path, monkeypatch):
    source = tmp_path / "cave.glb"
    source.write_bytes(b"glTF")
    cache_root = tmp_path / "cache-root"

    monkeypatch.setattr(
        map_compiler,
        "import_and_cache_any",
        lambda *_args, **_kwargs: pytest.fail("dry-run must not import"),
    )

    result = map_compiler.compile_map(
        map_compiler.CompileOptions(
            source=str(source),
            cache_root=str(cache_root),
            parsing_overrides={"chunk_size_meters": "32"},
            dry_run=True,
        )
    )

    assert result.status == "planned"
    assert result.dry_run is True
    assert result.cache_root == str(cache_root)
    assert result.cache_dir.startswith(str(cache_root))
    assert result.chunk_size == 32.0
    assert not cache_root.exists()


def test_compile_ignores_saved_gui_preferences_by_default(tmp_path):
    source = tmp_path / "cave.glb"
    source.write_bytes(b"glTF")
    cache_root = tmp_path / "cache-root"
    saved_preferences = tmp_path / "config" / "caveviewer" / "advanced_settings.json"
    saved_preferences.parent.mkdir(parents=True, exist_ok=True)
    saved_preferences.write_text(
        json.dumps({"chunk_size_meters": "99"}),
        encoding="utf-8",
    )

    result = map_compiler.compile_map(
        map_compiler.CompileOptions(
            source=str(source),
            cache_root=str(cache_root),
            dry_run=True,
        )
    )

    assert result.status == "planned"
    assert result.chunk_size == 50.0


def test_compile_uses_explicit_partial_settings_file(tmp_path):
    source = tmp_path / "cave.glb"
    source.write_bytes(b"glTF")
    cache_root = tmp_path / "cache-root"
    settings_file = tmp_path / "chunker-settings.json"
    settings_file.write_text(
        json.dumps({"chunk_size_meters": "72"}),
        encoding="utf-8",
    )

    result = map_compiler.compile_map(
        map_compiler.CompileOptions(
            source=str(source),
            cache_root=str(cache_root),
            settings_file=str(settings_file),
            dry_run=True,
        )
    )

    assert result.status == "planned"
    assert result.chunk_size == 72.0


def test_analyze_chunk_sizes_resolves_source_and_preferences(
    tmp_path, monkeypatch
):
    source = tmp_path / "cave.obj"
    source.write_text("v 0 0 0\n", encoding="utf-8")
    cache_root = tmp_path / "cache-root"
    captured = {}

    from caveviewer.core.map import chunk_size_advisor

    monkeypatch.delenv("CAVEVIEWER_MAP_CACHE_DIR", raising=False)
    monkeypatch.delenv("CAVEVIEWER_OBJ_IMPORT_BATCH_FACES", raising=False)
    monkeypatch.delenv("CAVEVIEWER_OBJ_BUCKET_WORKERS", raising=False)

    def fake_find_model_file(selected_path):
        captured["selected_path"] = selected_path
        return {"format": "obj", "obj_path": str(source)}

    def fake_recommend(
        model_descriptor,
        *,
        candidate_sizes,
        face_batch_size,
        worker_count,
        progress_cb=None,
    ):
        captured["model_descriptor"] = model_descriptor
        captured["candidate_sizes"] = candidate_sizes
        captured["face_batch_size"] = face_batch_size
        captured["worker_count"] = worker_count
        captured["progress_cb"] = progress_cb
        captured["environ"] = os.environ.copy()
        if progress_cb is not None:
            progress_cb("analyzing faces", 0.75)
        return ChunkSizeRecommendation(
            recommended_size=73.0,
            candidates=(),
            explanation="test recommendation",
        )

    monkeypatch.setattr(map_compiler, "find_model_file", fake_find_model_file)
    monkeypatch.setattr(
        chunk_size_advisor,
        "recommend_chunk_size_for_descriptor",
        fake_recommend,
    )
    monkeypatch.setattr(
        map_compiler,
        "import_and_cache_any",
        lambda *_args, **_kwargs: pytest.fail("analysis must not import"),
    )
    progress = []

    result = map_compiler.analyze_chunk_sizes(
        map_compiler.CompileOptions(
            source=str(source),
            cache_root=str(cache_root),
            parsing_overrides={
                "chunk_size_meters": "73",
                "obj_import_batch_thousands": "250",
            },
            obj_bucket_workers="4",
            analyze_workers="3",
        ),
        progress_cb=lambda *item: progress.append(item),
    )

    assert result.recommended_size == 73.0
    assert captured["selected_path"] == str(source)
    assert captured["model_descriptor"] == {"format": "obj", "obj_path": str(source)}
    assert 73.0 in captured["candidate_sizes"]
    assert captured["face_batch_size"] == 250000
    assert captured["worker_count"] == 3
    assert captured["progress_cb"] is not None
    assert progress == [("locating source", 0.0), ("analyzing faces", 0.75)]
    assert "CAVEVIEWER_MAP_CACHE_DIR" not in os.environ
    assert "CAVEVIEWER_OBJ_IMPORT_BATCH_FACES" not in os.environ
    assert "CAVEVIEWER_MAP_CACHE_DIR" not in captured["environ"]
    assert "CAVEVIEWER_OBJ_IMPORT_BATCH_FACES" not in captured["environ"]
    assert "CAVEVIEWER_OBJ_BUCKET_WORKERS" not in captured["environ"]


def test_compile_defaults_obj_bucket_workers_to_two(tmp_path, monkeypatch):
    source = tmp_path / "cave.glb"
    source.write_bytes(b"glTF")
    cache_root = tmp_path / "cache-root"

    captured_kwargs = []

    def fake_import(_model_descriptor, _textures_dir, **_kwargs):
        captured_kwargs.append(_kwargs)
        return str(cache_root / "built-cache")

    monkeypatch.setattr(map_compiler, "import_and_cache_any", fake_import)

    result = map_compiler.compile_map(
        map_compiler.CompileOptions(
            source=str(source),
            cache_root=str(cache_root),
        )
    )

    assert result.status == "built"
    assert captured_kwargs[0]["obj_bucket_workers"] == 2


def test_compile_rejects_invalid_obj_bucket_workers(tmp_path):
    source = tmp_path / "cave.glb"
    source.write_bytes(b"glTF")

    with pytest.raises(
        map_compiler.MapCompileConfigurationError,
        match="--obj-bucket-workers must be at least 1",
    ):
        map_compiler.compile_map(
            map_compiler.CompileOptions(
                source=str(source),
                obj_bucket_workers="0",
            )
        )


def test_analyze_rejects_invalid_worker_count(tmp_path):
    source = tmp_path / "cave.glb"
    source.write_bytes(b"glTF")

    with pytest.raises(
        map_compiler.MapCompileConfigurationError,
        match="--analyze-workers must be at least 1",
    ):
        map_compiler.analyze_chunk_sizes(
            map_compiler.CompileOptions(
                source=str(source),
                analyze_workers="0",
            )
        )


def test_compile_rejects_relative_cache_root(tmp_path):
    source = tmp_path / "cave.glb"
    source.write_bytes(b"glTF")

    with pytest.raises(
        map_compiler.MapCompileConfigurationError,
        match="--cache-root must be an absolute path",
    ):
        map_compiler.compile_map(
            map_compiler.CompileOptions(
                source=str(source),
                cache_root="relative-cache",
            )
        )


def test_compile_rejects_invalid_explicit_settings_file(tmp_path):
    source = tmp_path / "cave.glb"
    source.write_bytes(b"glTF")
    settings_file = tmp_path / "settings.json"
    settings_file.write_text("not json", encoding="utf-8")

    with pytest.raises(
        map_compiler.MapCompileConfigurationError,
        match="Could not load --settings-file",
    ):
        map_compiler.compile_map(
            map_compiler.CompileOptions(
                source=str(source),
                settings_file=str(settings_file),
            )
        )


def test_compile_rejects_oversized_explicit_settings_file(tmp_path, monkeypatch):
    source = tmp_path / "cave.glb"
    source.write_bytes(b"glTF")
    settings_file = tmp_path / "settings.json"
    settings_file.write_text('{"chunk_size_meters": "72"}', encoding="utf-8")
    monkeypatch.setattr(map_compiler, "MAX_SETTINGS_FILE_BYTES", 8)

    with pytest.raises(
        map_compiler.MapCompileConfigurationError,
        match="Could not load --settings-file",
    ):
        map_compiler.compile_map(
            map_compiler.CompileOptions(
                source=str(source),
                settings_file=str(settings_file),
            )
        )
