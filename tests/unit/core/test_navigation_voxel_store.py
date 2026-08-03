"""Tests for navigation voxel chunk storage backends."""

from __future__ import annotations

import json

from caveviewer.core.navigation.voxel_cache import (
    deserialize_local_voxel_volume,
    serialize_local_voxel_volume,
)
from caveviewer.core.navigation.voxel_store import (
    DiskNavigationVoxelChunkStore,
    InMemoryNavigationVoxelChunkStore,
    NavigationVoxelChunkDescriptor,
    navigation_voxel_chunk_relative_path_parts,
)
from caveviewer.core.navigation.voxel_volume import LocalVoxelVolume


def _volume(origin_x: float) -> LocalVoxelVolume:
    return LocalVoxelVolume(
        voxel_size_m=1.0,
        origin=(origin_x, 0.0, 0.0),
        shape=(2, 2, 2),
        surface_cells=frozenset({(0, 0, 0)}),
        triangle_count=1,
        surface_sample_count=1,
        sampling_truncated=False,
        max_clearance_search_cells=4,
    )


def test_in_memory_store_keeps_all_chunks_resident():
    store = InMemoryNavigationVoxelChunkStore(
        coarse_tiles=(_volume(0.0),),
        fine_tiles=(_volume(4.0),),
    )

    assert len(store.descriptors()) == 2
    assert len(store.descriptors(fine_only=False)) == 1
    assert len(store.descriptors(fine_only=True)) == 1
    assert store.chunk_ids_for_point((0.5, 0.5, 0.5)) == ("coarse-000000",)
    assert store.get_chunk("fine-000000") == _volume(4.0)
    assert store.stats()["backend"] == "in_memory"
    assert store.stats()["resident_chunk_count"] == 2


def test_chunk_descriptor_and_payload_round_trip_quarter_metre_vertical_cells():
    volume = LocalVoxelVolume(
        voxel_size_m=1.0,
        vertical_voxel_size_m=0.25,
        origin=(0.0, 0.0, 0.0),
        shape=(2, 2, 2),
        surface_cells=frozenset({(0, 0, 0)}),
        triangle_count=1,
        surface_sample_count=1,
        sampling_truncated=False,
        max_clearance_search_cells=4,
    )
    descriptor = NavigationVoxelChunkDescriptor.from_volume(
        "coarse-000000",
        "coarse",
        volume,
        relative_path="chunks/quarter.json",
    )
    restored = deserialize_local_voxel_volume(
        serialize_local_voxel_volume(volume)
    )

    assert descriptor.cell_size_m == (1.0, 0.25, 1.0)
    assert descriptor.cell_volume_m3 == 0.25
    assert descriptor.bounds_max == (2.0, 0.5, 2.0)
    assert restored.vertical_voxel_size_m == 0.25
    assert restored.cell_size_m == (1.0, 0.25, 1.0)


def test_disk_store_loads_chunks_lazily_and_evicts_lru(tmp_path):
    first = _volume(0.0)
    second = _volume(4.0)
    descriptors = (
        NavigationVoxelChunkDescriptor.from_volume(
            "coarse-000000",
            "coarse",
            first,
            relative_path="chunks/first.json",
        ),
        NavigationVoxelChunkDescriptor.from_volume(
            "coarse-000001",
            "coarse",
            second,
            relative_path="chunks/second.json",
        ),
    )
    for descriptor, volume in zip(descriptors, (first, second)):
        path = tmp_path / descriptor.relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(serialize_local_voxel_volume(volume)))

    store = DiskNavigationVoxelChunkStore(
        tmp_path,
        descriptors,
        decoder=deserialize_local_voxel_volume,
        max_resident_chunks=1,
    )

    assert store.resident_chunk_ids() == ()
    assert store.get_chunk("coarse-000000") == first
    assert store.resident_chunk_ids() == ("coarse-000000",)
    assert store.get_chunk("coarse-000001") == second
    assert store.resident_chunk_ids() == ("coarse-000001",)
    assert store.get_chunk("coarse-000000") == first
    stats = store.stats()
    assert stats["backend"] == "disk_lru"
    assert stats["evictions"] == 2
    assert stats["cache_misses"] == 3


def test_disk_store_rejects_chunk_path_escape(tmp_path):
    volume = _volume(0.0)
    descriptor = NavigationVoxelChunkDescriptor.from_volume(
        "coarse-000000",
        "coarse",
        volume,
        relative_path="../outside.json",
    )
    store = DiskNavigationVoxelChunkStore(
        tmp_path,
        (descriptor,),
        decoder=lambda payload: volume,
    )

    assert store.get_chunk("coarse-000000") is None
    assert store.stats()["load_errors"] == 1


def test_chunk_paths_use_portable_posix_cache_components():
    relative_path = (
        "navigation_voxel_chunks/route-deadbeef/fine-000000.json"
    )

    assert navigation_voxel_chunk_relative_path_parts(relative_path) == (
        "navigation_voxel_chunks",
        "route-deadbeef",
        "fine-000000.json",
    )
    assert (
        navigation_voxel_chunk_relative_path_parts(
            r"navigation_voxel_chunks\route-deadbeef\fine-000000.json"
        )
        is None
    )
    assert (
        navigation_voxel_chunk_relative_path_parts(
            "navigation_voxel_chunks/../outside.json"
        )
        is None
    )
