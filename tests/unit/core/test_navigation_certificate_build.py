"""Tests for explicit, render-cache-independent navigation certification."""

from __future__ import annotations

import json

import numpy as np
import pytest

from caveviewer.core.chunking import builder as chunker
from caveviewer.core.map.cache_identity import build_guided_dive_cache_identity
from caveviewer.core.navigation import certificate_build
from caveviewer.core.navigation.voxel_cache import (
    NAVIGATION_VOXEL_CACHE_METHOD,
    NAVIGATION_VOXEL_CACHE_NAME,
    NAVIGATION_VOXEL_CACHE_VERSION,
    NavigationVoxelCacheBuildResult,
)


def _render_cache(tmp_path):
    source = tmp_path / "map.obj"
    source.write_text("v 0 0 0\n", encoding="utf-8")
    cache_dir = tmp_path / "_cache"
    (cache_dir / "chunks").mkdir(parents=True)
    manifest = {
        "version": chunker._VERSION,
        "chunk_size": 8.0,
        "source_obj": source.name,
        "triangle_count": 1,
        "chunks": {
            "0_0_0": {
                "bounds_min": [0.0, 0.0, 0.0],
                "bounds_max": [8.0, 8.0, 8.0],
            }
        },
    }
    manifest["guided_dive_identity"] = build_guided_dive_cache_identity(
        source,
        manifest,
    ).payload()
    manifest_path = cache_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    return source, cache_dir, manifest_path, manifest


def _patch_successful_certificate_build(monkeypatch):
    navigation_metadata = {
        "method": "footprint_centerline_paths_v1",
        "routes": [{"id": "main"}],
        "recommended_route_id": "main",
    }

    class FakeGuard:
        def triangle_meshes_for_bounds(self, _lower, _upper):
            return ()

        def segment_collision(self, _first, _second):
            return None

        def opposing_axis_support(
            self,
            _point,
            *,
            max_distance_m,
            minimum_clearance_m,
        ):
            assert max_distance_m == 128.0
            assert minimum_clearance_m == 0.25
            return True

    class FakeGuardFactory:
        @staticmethod
        def from_manifest(manifest, *, cache_dir):
            assert manifest["navigation"] is navigation_metadata
            assert cache_dir.endswith("_cache")
            return FakeGuard()

    def fake_voxel_build(
        _manifest,
        navigation,
        *,
        triangle_provider,
        mesh_edge_is_clear,
        mesh_point_has_opposing_support,
        progress_cb,
    ):
        assert navigation is navigation_metadata
        assert triangle_provider((0.0, 0.0, 0.0), (1.0, 1.0, 1.0)) == ()
        assert mesh_edge_is_clear((0.0, 0.0, 0.0), (1.0, 1.0, 1.0)) is True
        assert mesh_point_has_opposing_support((0.5, 0.5, 0.5), 128.0, 0.25)
        progress_cb("certifying navigation route 1/1", 0.5)
        navigation["voxel_cache"] = {
            "version": NAVIGATION_VOXEL_CACHE_VERSION,
            "method": NAVIGATION_VOXEL_CACHE_METHOD,
            "path": NAVIGATION_VOXEL_CACHE_NAME,
            "chunk_directory": "navigation_voxel_chunks",
            "built_route_count": 1,
        }
        return NavigationVoxelCacheBuildResult(
            payload={"routes": {}},
            chunked_payload={"routes": {"main": {"model": "chunked"}}},
            chunk_payloads={
                "navigation_voxel_chunks/main.json": {"route": "main"}
            },
            built_route_count=1,
            recommended_route_id="main",
        )

    monkeypatch.setattr(
        certificate_build,
        "_load_surface_positions",
        lambda *_args, **_kwargs: np.asarray(((0.0, 0.0, 0.0),)),
    )
    monkeypatch.setattr(
        certificate_build,
        "build_navigation_metadata",
        lambda *_args, **_kwargs: navigation_metadata,
    )
    monkeypatch.setattr(
        certificate_build,
        "CachedChunkMeshCollisionGuard",
        FakeGuardFactory,
    )
    monkeypatch.setattr(
        certificate_build,
        "build_navigation_voxel_cache",
        fake_voxel_build,
    )


def test_certificate_build_publishes_separate_bound_artifacts(tmp_path, monkeypatch):
    source, cache_dir, manifest_path, manifest = _render_cache(tmp_path)
    manifest_before = manifest_path.read_bytes()
    _patch_successful_certificate_build(monkeypatch)
    events: list[tuple[str, float]] = []

    result = certificate_build.build_navigation_certificate(
        source,
        cache_dir=cache_dir,
        progress_cb=lambda stage, fraction: events.append((stage, fraction)),
    )

    certificate_dir = cache_dir / "navigation_certificate"
    assert result.status == "built"
    assert result.route_count == 1
    assert manifest_path.read_bytes() == manifest_before
    assert "navigation" not in json.loads(manifest_before)
    assert (certificate_dir / "certificate.json").is_file()
    assert (certificate_dir / "navigation_voxels.json").is_file()
    assert (certificate_dir / "navigation_voxel_chunks" / "main.json").is_file()
    payload = json.loads((certificate_dir / "certificate.json").read_text())
    assert payload["render_cache_identity"] == manifest["guided_dive_identity"]
    assert payload["navigation"]["voxel_cache"]["path"] == (
        "navigation_certificate/navigation_voxels.json"
    )
    assert payload["navigation"]["voxel_cache"]["chunk_directory"] == (
        "navigation_certificate/navigation_voxel_chunks"
    )
    assert certificate_build.load_navigation_certificate(
        manifest,
        cache_dir=cache_dir,
    ) == payload["navigation"]
    effective_manifest = certificate_build.manifest_with_navigation_certificate(
        manifest,
        cache_dir=cache_dir,
    )
    assert effective_manifest["navigation"] == payload["navigation"]
    assert "navigation" not in manifest
    assert [fraction for _stage, fraction in events] == sorted(
        fraction for _stage, fraction in events
    )
    assert events[0] == ("validating render cache", 0.0)
    assert events[-1] == ("done", 1.0)


def test_certificate_build_skips_matching_published_artifact(tmp_path, monkeypatch):
    source, cache_dir, _manifest_path, _manifest = _render_cache(tmp_path)
    _patch_successful_certificate_build(monkeypatch)
    first = certificate_build.build_navigation_certificate(source, cache_dir=cache_dir)
    assert first.status == "built"

    monkeypatch.setattr(
        certificate_build,
        "_load_surface_positions",
        lambda *_args, **_kwargs: pytest.fail("current certificate was rebuilt"),
    )
    second = certificate_build.build_navigation_certificate(source, cache_dir=cache_dir)

    assert second.status == "skipped"
    assert second.route_count == 1


def test_certificate_loader_rejects_an_identity_from_another_render_cache(
    tmp_path,
    monkeypatch,
):
    source, cache_dir, _manifest_path, manifest = _render_cache(tmp_path)
    _patch_successful_certificate_build(monkeypatch)
    certificate_build.build_navigation_certificate(source, cache_dir=cache_dir)
    stale_manifest = dict(manifest)
    stale_manifest["guided_dive_identity"] = {
        **manifest["guided_dive_identity"],
        "source_sha256": "f" * 64,
    }

    assert certificate_build.load_navigation_certificate(
        stale_manifest,
        cache_dir=cache_dir,
    ) is None


def test_failed_certificate_rebuild_preserves_previous_artifacts(tmp_path, monkeypatch):
    source, cache_dir, _manifest_path, _manifest = _render_cache(tmp_path)
    _patch_successful_certificate_build(monkeypatch)
    certificate_build.build_navigation_certificate(source, cache_dir=cache_dir)
    certificate_path = cache_dir / "navigation_certificate" / "certificate.json"
    before = certificate_path.read_bytes()

    monkeypatch.setattr(
        certificate_build,
        "build_navigation_voxel_cache",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    with pytest.raises(
        certificate_build.NavigationCertificateBuildError,
        match="route certification failed",
    ):
        certificate_build.build_navigation_certificate(
            source,
            cache_dir=cache_dir,
            force=True,
        )

    assert certificate_path.read_bytes() == before


def test_certificate_requires_stable_render_identity(tmp_path):
    source, cache_dir, manifest_path, manifest = _render_cache(tmp_path)
    manifest.pop("guided_dive_identity")
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(
        certificate_build.NavigationCertificateBuildError,
        match="stable Guided Dive identity",
    ):
        certificate_build.build_navigation_certificate(source, cache_dir=cache_dir)


def test_certificate_entrance_helpers_remain_offline_only(tmp_path):
    source = tmp_path / "map.obj"
    source.write_bytes(b"small source map")
    chunks = {
        "19_0_-16": {
            "bounds_min": [950.0, -25.0, -800.0],
            "bounds_max": [1000.0, 25.0, -750.0],
        },
        "-3_-1_0": {
            "bounds_min": [-140.0, -10.0, 0.0],
            "bounds_max": [-100.0, 0.0, 50.0],
        },
    }

    navigation_start, navigation_start_anchor = (
        certificate_build._navigation_start_metadata_for_source(
            str(source),
            np.asarray(((987.0, -16.0, -816.0),), dtype=np.float32),
            manifest_chunks=chunks,
        )
    )

    assert navigation_start is None
    assert navigation_start_anchor == {
        "position": [987.0, -16.0, -816.0],
        "kind": "obj_surface_vertex",
        "source": "map.obj",
        "source_vertex_index": 0,
        "source_order": "obj_declaration_order",
        "executable": False,
        "attachment_required": True,
        "attachment_coordinate_space": "xyz",
    }


def test_certificate_entrance_sidecar_remains_non_obj_fallback(tmp_path):
    source = tmp_path / "map.glb"
    source.write_bytes(b"small source map")
    (tmp_path / "map.navigation.json").write_text(
        json.dumps(
            {
                "navigation_start": {
                    "position": [7.0, 0.0, 0.0],
                    "label": "entrance",
                }
            }
        ),
        encoding="utf-8",
    )

    navigation_start, navigation_start_anchor = (
        certificate_build._navigation_start_metadata_for_source(
            str(source),
            np.asarray(((0.0, 0.0, 0.0),)),
        )
    )

    assert navigation_start == {
        "navigation_start": {
            "position": [7.0, 0.0, 0.0],
            "label": "entrance",
        },
        "source": "map.navigation.json",
    }
    assert navigation_start_anchor is None


def test_certificate_cli_reports_machine_readable_result(monkeypatch, capsys):
    result = certificate_build.NavigationCertificateBuildResult(
        status="built",
        source_path="/maps/cave.obj",
        source_format="obj",
        cache_dir="/maps/_cache",
        certificate_dir="/maps/_cache/navigation_certificate",
        route_count=1,
        recommended_route_id="main",
        elapsed_seconds=1.25,
    )
    monkeypatch.setattr(
        certificate_build,
        "resolve_navigation_certificate_source",
        lambda _value: ("/maps/cave.obj", "obj"),
    )
    monkeypatch.setattr(
        certificate_build,
        "build_navigation_certificate",
        lambda *_args, **_kwargs: result,
    )

    assert certificate_build.main(
        ["--source=/maps/cave.obj", "--cache-dir=/maps/_cache", "--json"]
    ) == 0

    assert json.loads(capsys.readouterr().out) == result.as_dict()
