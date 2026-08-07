"""Validate Map Library cache-rebuild capability facts and policy decisions."""

from __future__ import annotations

from pathlib import Path

from caveviewer.core.capabilities import CapabilityStatus
from caveviewer.core.chunking import builder as chunker
from caveviewer.core.chunking.io import CHUNKS_DIRNAME, _VERSION
from caveviewer.core.chunking.staging import ResumableObjImport
from caveviewer.core.map.cache_build_lock import CacheBuildLock
from caveviewer.gui import map_cache_rebuild
from caveviewer.gui.features import FeatureState


def _map_with_source(tmp_path):
    map_dir = tmp_path / "maps" / "cave"
    map_dir.mkdir(parents=True)
    source = map_dir / "cave.glb"
    source.write_bytes(b"glTF")
    return map_dir, source, map_dir / "_cache"


def test_preflight_allows_build_over_an_existing_stale_generated_cache(tmp_path):
    map_dir, source, cache_dir = _map_with_source(tmp_path)
    cache_dir.mkdir()
    (cache_dir / "stale-marker").write_text("stale", encoding="utf-8")

    preflight = map_cache_rebuild.probe_map_library_cache_rebuild(map_dir)

    assert preflight.capability.status is CapabilityStatus.AVAILABLE
    assert preflight.decision.allows_execution
    assert preflight.capability.value is not None
    assert preflight.capability.value.cache_dir == cache_dir
    assert preflight.capability.value.source_path == source
    assert preflight.capability.value.textures_dir == map_dir
    assert preflight.capability.value.operation == "build"


def test_preflight_allows_build_when_no_generated_cache_exists(tmp_path):
    map_dir, _source, _cache_dir = _map_with_source(tmp_path)

    preflight = map_cache_rebuild.probe_map_library_cache_rebuild(map_dir)

    assert preflight.capability.status is CapabilityStatus.AVAILABLE
    assert preflight.decision.state is FeatureState.ENABLED
    assert preflight.decision.reason_code == "map_cache_build_available"
    assert preflight.capability.value is not None
    assert preflight.capability.value.operation == "build"


def test_preflight_allows_rebuild_of_a_valid_generated_cache(tmp_path):
    map_dir, source, cache_dir = _map_with_source(tmp_path)
    chunks_dir = cache_dir / CHUNKS_DIRNAME
    chunks_dir.mkdir(parents=True)
    (cache_dir / chunker.MANIFEST_NAME).write_text(
        f'{{"version": {_VERSION}, "chunks": {{}}}}',
        encoding="utf-8",
    )

    preflight = map_cache_rebuild.probe_map_library_cache_rebuild(map_dir)

    assert preflight.capability.status is CapabilityStatus.AVAILABLE
    assert preflight.decision.allows_execution
    assert preflight.decision.reason_code == "map_cache_rebuild_available"
    assert preflight.capability.value is not None
    assert preflight.capability.value.cache_dir == cache_dir
    assert preflight.capability.value.source_path == source
    assert preflight.capability.value.operation == "rebuild"


def test_preflight_disables_rebuild_when_only_a_cache_remains(tmp_path):
    map_dir = tmp_path / "maps" / "cache-only"
    cache_dir = map_dir / "_cache"
    cache_dir.mkdir(parents=True)

    preflight = map_cache_rebuild.probe_map_library_cache_rebuild(map_dir)

    assert preflight.capability.status is CapabilityStatus.UNAVAILABLE
    assert preflight.decision.state is FeatureState.DISABLED
    assert preflight.decision.reason_code == "map_cache_rebuild_source_unavailable"


def test_preflight_disables_precompiled_cache_entry(tmp_path):
    cache_dir = tmp_path / "precompiled"
    cache_dir.mkdir()
    (cache_dir / chunker.MANIFEST_NAME).write_text("{}", encoding="utf-8")

    preflight = map_cache_rebuild.probe_map_library_cache_rebuild(cache_dir)

    assert preflight.capability.status is CapabilityStatus.UNAVAILABLE
    assert preflight.decision.state is FeatureState.DISABLED
    assert preflight.decision.reason_code == "map_cache_rebuild_precompiled_map"


def test_preflight_disables_unsafe_or_busy_generated_destination(tmp_path):
    map_dir, _source, cache_dir = _map_with_source(tmp_path)
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    cache_dir.symlink_to(outside_dir, target_is_directory=True)

    unsafe = map_cache_rebuild.probe_map_library_cache_rebuild(map_dir)

    assert unsafe.decision.state is FeatureState.DISABLED
    assert unsafe.decision.reason_code == "map_cache_rebuild_destination_unsafe"

    cache_dir.unlink()
    cache_dir.mkdir()
    lock = CacheBuildLock(cache_dir)
    lock.acquire()
    try:
        busy = map_cache_rebuild.probe_map_library_cache_rebuild(map_dir)
    finally:
        lock.release()

    assert busy.decision.state is FeatureState.DISABLED
    assert busy.decision.reason_code == "map_cache_rebuild_already_in_progress"


def test_preflight_attaches_a_validated_resume_fact_and_keeps_it_while_busy(
    tmp_path,
    monkeypatch,
):
    map_dir = tmp_path / "maps" / "cave"
    map_dir.mkdir(parents=True)
    source = map_dir / "cave.obj"
    source.write_text("mtllib cave.mtl\nv 0 0 0\n", encoding="utf-8")
    material = map_dir / "cave.mtl"
    material.write_text("newmtl rock\n", encoding="utf-8")
    cache_dir = map_dir / "_cache"
    cache_dir.mkdir()
    resumable = ResumableObjImport(
        resume_dir=Path("/maps/.cache.resume-123"),
        stage="bucketing",
        progress_fraction=0.4,
    )
    calls = []

    def probe(descriptor, *, cache_dir):
        calls.append((descriptor, cache_dir))
        return resumable

    monkeypatch.setattr(map_cache_rebuild, "probe_resumable_import", probe)

    available = map_cache_rebuild.probe_map_library_cache_rebuild(map_dir)

    assert available.resumable_import is resumable
    assert available.decision.allows_execution
    assert calls == [
        (
            {
                "format": "obj",
                "obj_path": str(source),
                "mtl_path": str(material),
            },
            str(cache_dir),
        )
    ]

    lock = CacheBuildLock(cache_dir)
    lock.acquire()
    try:
        busy = map_cache_rebuild.probe_map_library_cache_rebuild(map_dir)
    finally:
        lock.release()

    assert busy.resumable_import is resumable
    assert busy.decision.state is FeatureState.DISABLED
    assert busy.decision.reason_code == "map_cache_rebuild_already_in_progress"


def test_preflight_disables_a_generated_cache_when_its_source_cannot_be_read(
    tmp_path,
    monkeypatch,
):
    map_dir, _source, cache_dir = _map_with_source(tmp_path)
    cache_dir.mkdir()
    monkeypatch.setattr(
        map_cache_rebuild,
        "_sources_are_readable",
        lambda _descriptor: False,
    )

    preflight = map_cache_rebuild.probe_map_library_cache_rebuild(map_dir)

    assert preflight.capability.status is CapabilityStatus.UNAVAILABLE
    assert preflight.decision.state is FeatureState.DISABLED
    assert preflight.decision.reason_code == "map_cache_rebuild_source_unreadable"
