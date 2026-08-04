"""Cover source-model discovery and minimal GLB parsing behavior."""

from __future__ import annotations

import builtins
import io
import os

import numpy as np
import pytest

from caveviewer import app
from caveviewer.app import find_input_files, find_model_file
from caveviewer.core.capabilities import CapabilityStatus
from caveviewer.core.map import source_model
from caveviewer.core.mesh.glb import parse_glb


def test_source_format_registry_declares_supported_import_and_package_metadata():
    formats = source_model.supported_source_formats()

    assert formats == (
        source_model.OBJ_SOURCE_FORMAT,
        source_model.GLB_SOURCE_FORMAT,
    )
    assert [source_format.id.value for source_format in formats] == ["obj", "glb"]
    assert [source_format.extension for source_format in formats] == [".obj", ".glb"]
    assert [source_format.mime_type for source_format in formats] == [
        "model/obj",
        "model/gltf-binary",
    ]
    assert source_model.supported_source_format_summary() == (
        ".obj (with a matching .mtl) and .glb"
    )
    assert source_model.supported_source_format_summary(conjunction="or") == (
        ".obj (with a matching .mtl) or .glb"
    )


def test_source_format_capability_classifies_paths_and_descriptors():
    glb_capability = source_model.probe_source_format("/maps/cave.GLB")
    unsupported_capability = source_model.probe_source_format("/maps/notes.txt")
    descriptor_capability = source_model.probe_model_descriptor({"format": "obj"})
    unknown_descriptor_capability = source_model.probe_model_descriptor(
        {"format": "ply"}
    )

    assert glb_capability.status is CapabilityStatus.AVAILABLE
    assert glb_capability.value is source_model.GLB_SOURCE_FORMAT
    assert glb_capability.evidence == {"extension": ".glb", "format": "glb"}
    assert unsupported_capability.status is CapabilityStatus.UNAVAILABLE
    assert unsupported_capability.reason_code == "map_source_format_unsupported"
    assert unsupported_capability.evidence == {"extension": ".txt"}
    assert descriptor_capability.status is CapabilityStatus.AVAILABLE
    assert descriptor_capability.value is source_model.OBJ_SOURCE_FORMAT
    assert unknown_descriptor_capability.status is CapabilityStatus.UNAVAILABLE
    assert unknown_descriptor_capability.evidence == {"format": "ply"}


def test_find_supported_source_files_uses_the_format_registry(tmp_path):
    obj = tmp_path / "cave.obj"
    glb = tmp_path / "cave.glb"
    obj.write_text("v 0 0 0\n", encoding="utf-8")
    glb.write_bytes(b"glTF")

    candidates = source_model.find_supported_source_files(str(tmp_path))

    assert candidates == (
        source_model.SourceModelCandidate(source_model.OBJ_SOURCE_FORMAT, str(obj)),
        source_model.SourceModelCandidate(source_model.GLB_SOURCE_FORMAT, str(glb)),
    )


def test_find_model_rejects_missing_directory(tmp_path):
    missing = tmp_path / "does-not-exist"
    with pytest.raises(FileNotFoundError, match="No supported model file"):
        find_model_file(str(missing))


def test_find_model_rejects_empty_directory(tmp_path):
    with pytest.raises(FileNotFoundError, match="No supported model file"):
        find_model_file(str(tmp_path))


def test_find_input_files_rejects_missing_obj(tmp_path):
    (tmp_path / "cave.mtl").write_text("newmtl rock\n", encoding="utf-8")
    with pytest.raises(FileNotFoundError, match=r"No \.obj file"):
        find_input_files(str(tmp_path))


def test_find_input_files_rejects_obj_without_mtl(tmp_path):
    (tmp_path / "cave.obj").write_text("v 0 0 0\n", encoding="utf-8")
    with pytest.raises(FileNotFoundError, match=r"no matching \.mtl"):
        find_input_files(str(tmp_path))


def test_find_input_files_rejects_missing_referenced_mtl(tmp_path):
    (tmp_path / "cave.obj").write_text(
        "mtllib missing.mtl\nv 0 0 0\n", encoding="utf-8"
    )
    with pytest.raises(FileNotFoundError, match=r"no matching \.mtl"):
        find_input_files(str(tmp_path))


def test_find_input_files_uses_referenced_mtl(tmp_path):
    obj = tmp_path / "cave.obj"
    mtl = tmp_path / "materials.mtl"
    obj.write_text("mtllib materials.mtl\nv 0 0 0\n", encoding="utf-8")
    mtl.write_text("newmtl rock\n", encoding="utf-8")
    assert find_input_files(str(tmp_path)) == (str(obj), str(mtl))


def test_declared_material_lookup_uses_bounded_header_read(tmp_path, monkeypatch):
    obj = tmp_path / "cave.obj"
    material = tmp_path / "cave.mtl"
    obj.write_bytes(b"placeholder")
    material.write_text("newmtl rock\n", encoding="utf-8")

    read_sizes = []

    class TrackingReader(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, _exc_type, _exc, _traceback):
            return False

        def read(self, size=-1):
            read_sizes.append(size)
            if size < 0 or size > 32:
                raise AssertionError("OBJ discovery attempted an unbounded read")
            return super().read(size)

    reader = TrackingReader(b"mtllib cave.mtl\n" + (b"v 0 0 0\n" * 100))
    real_open = builtins.open

    def fake_open(path, *args, **kwargs):
        if os.fspath(path) == str(obj):
            return reader
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", fake_open)

    assert (
        source_model.find_declared_material_file_for_obj(
            str(obj),
            max_scan_bytes=32,
        )
        == str(material)
    )
    assert read_sizes == [32]


def test_declared_material_lookup_ignores_directive_after_scan_limit(tmp_path):
    obj = tmp_path / "cave.obj"
    material_dir = tmp_path / "materials"
    material_dir.mkdir()
    material = material_dir / "late.mtl"
    obj.write_text(
        "v 0 0 0\nmtllib materials/late.mtl\n",
        encoding="utf-8",
    )
    material.write_text("newmtl rock\n", encoding="utf-8")

    assert (
        source_model.find_declared_material_file_for_obj(
            str(obj),
            max_scan_bytes=len("v 0 0 0\n"),
        )
        is None
    )


def test_find_input_files_falls_back_to_available_mtl(tmp_path):
    obj = tmp_path / "cave.obj"
    fallback = tmp_path / "fallback.mtl"
    obj.write_text("mtllib missing.mtl\nv 0 0 0\n", encoding="utf-8")
    fallback.write_text("newmtl rock\n", encoding="utf-8")
    assert find_input_files(str(tmp_path)) == (str(obj), str(fallback))


def test_find_input_files_rejects_unsafe_referenced_mtl_path(tmp_path):
    (tmp_path / "cave.obj").write_text(
        "mtllib ../outside.mtl\nv 0 0 0\n", encoding="utf-8"
    )
    (tmp_path / "fallback.mtl").write_text("newmtl fallback\n", encoding="utf-8")

    with pytest.raises(ValueError, match="Unsafe OBJ material path"):
        find_input_files(str(tmp_path))


def test_find_input_files_reports_multiple_obj_candidates(tmp_path, monkeypatch):
    first = tmp_path / "first.obj"
    second = tmp_path / "second.obj"
    material = tmp_path / "map.mtl"
    first.write_text("mtllib map.mtl\n", encoding="utf-8")
    second.write_text("mtllib map.mtl\n", encoding="utf-8")
    material.write_text("newmtl rock\n", encoding="utf-8")

    messages = []
    monkeypatch.setattr(app._LOG, "info", messages.append)

    obj_path, mtl_path = find_input_files(str(tmp_path))

    assert obj_path in {str(first), str(second)}
    assert mtl_path == str(material)
    assert any("multiple .obj files found" in message for message in messages)


def test_find_model_returns_glb_descriptor_without_companion_files(tmp_path):
    glb = tmp_path / "cave.glb"
    glb.write_bytes(b"glTF")
    assert find_model_file(str(tmp_path)) == {"format": "glb", "glb_path": str(glb)}


def test_find_model_accepts_direct_glb_file(tmp_path):
    glb = tmp_path / "cave.glb"
    glb.write_bytes(b"glTF")

    assert find_model_file(str(glb)) == {"format": "glb", "glb_path": str(glb)}


def test_find_model_accepts_direct_obj_file_without_guessing_another_obj(tmp_path):
    selected = tmp_path / "selected.obj"
    other = tmp_path / "other.obj"
    selected_mtl = tmp_path / "selected.mtl"
    other_mtl = tmp_path / "other.mtl"
    selected.write_text("mtllib selected.mtl\n", encoding="utf-8")
    other.write_text("mtllib other.mtl\n", encoding="utf-8")
    selected_mtl.write_text("newmtl selected\n", encoding="utf-8")
    other_mtl.write_text("newmtl other\n", encoding="utf-8")

    assert find_model_file(str(selected)) == {
        "format": "obj",
        "obj_path": str(selected),
        "mtl_path": str(selected_mtl),
    }


def test_find_model_rejects_direct_unsupported_file(tmp_path):
    text_file = tmp_path / "notes.txt"
    text_file.write_text("not a map", encoding="utf-8")

    with pytest.raises(FileNotFoundError, match="No supported model file"):
        find_model_file(str(text_file))


def test_find_model_reports_multiple_glb_candidates(tmp_path, monkeypatch):
    first = tmp_path / "first.glb"
    second = tmp_path / "second.glb"
    first.write_bytes(b"glTF")
    second.write_bytes(b"glTF")

    messages = []
    monkeypatch.setattr(app._LOG, "info", messages.append)

    descriptor = find_model_file(str(tmp_path))

    assert descriptor["glb_path"] in {str(first), str(second)}
    assert any("multiple .glb files found" in message for message in messages)


def test_find_model_prefers_obj_when_obj_and_glb_exist(tmp_path):
    obj = tmp_path / "cave.obj"
    mtl = tmp_path / "cave.mtl"
    obj.write_text("mtllib cave.mtl\n", encoding="utf-8")
    mtl.write_text("newmtl rock\n", encoding="utf-8")
    (tmp_path / "cave.glb").write_bytes(b"glTF")

    descriptor = find_model_file(str(tmp_path))

    assert descriptor["format"] == "obj"
    assert os.path.samefile(descriptor["obj_path"], obj)
    assert os.path.samefile(descriptor["mtl_path"], mtl)


def test_glb_parser_dependency_is_packaged():
    from pygltflib import GLTF2

    assert GLTF2 is not None


def test_glb_parser_rejects_missing_file(tmp_path):
    with pytest.raises(FileNotFoundError):
        parse_glb(str(tmp_path / "missing.glb"))


def test_glb_parser_reads_minimal_real_file(tmp_path):
    from pygltflib import (
        Accessor,
        Asset,
        Attributes,
        Buffer,
        BufferView,
        GLTF2,
        Mesh,
        Node,
        Primitive,
        Scene,
    )

    positions = np.array(
        [[0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [0.0, 1.0, 0.0]],
        dtype=np.float32,
    )
    gltf = GLTF2(
        asset=Asset(version="2.0"),
        scenes=[Scene(nodes=[0])],
        scene=0,
        nodes=[Node(mesh=0)],
        meshes=[Mesh(primitives=[Primitive(attributes=Attributes(POSITION=0))])],
        buffers=[Buffer(byteLength=positions.nbytes)],
        bufferViews=[
            BufferView(
                buffer=0,
                byteOffset=0,
                byteLength=positions.nbytes,
                target=34962,
            )
        ],
        accessors=[
            Accessor(
                bufferView=0,
                byteOffset=0,
                componentType=5126,
                count=3,
                type="VEC3",
                min=[0.0, 0.0, 0.0],
                max=[1.0, 1.0, 0.0],
            )
        ],
    )
    gltf.set_binary_blob(positions.tobytes())
    path = tmp_path / "minimal.glb"
    gltf.save_binary(str(path))

    preflight_counts = []
    mesh, embedded_images = parse_glb(
        str(path),
        preflight_cb=lambda *counts: preflight_counts.append(counts),
    )

    assert preflight_counts == [(3, 3, 3, 1)]
    np.testing.assert_array_equal(mesh.positions, positions)
    np.testing.assert_array_equal(mesh.face_pos_idx, [[0, 1, 2]])
    assert embedded_images == {}


def test_glb_parser_rejects_accessor_range_outside_buffer_view(tmp_path):
    from pygltflib import (
        Accessor,
        Asset,
        Attributes,
        Buffer,
        BufferView,
        GLTF2,
        Mesh,
        Node,
        Primitive,
        Scene,
    )

    positions = np.array(
        [[0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [0.0, 1.0, 0.0]],
        dtype=np.float32,
    )
    gltf = GLTF2(
        asset=Asset(version="2.0"),
        scenes=[Scene(nodes=[0])],
        scene=0,
        nodes=[Node(mesh=0)],
        meshes=[Mesh(primitives=[Primitive(attributes=Attributes(POSITION=0))])],
        buffers=[Buffer(byteLength=positions.nbytes)],
        bufferViews=[
            BufferView(
                buffer=0,
                byteOffset=0,
                byteLength=positions.nbytes - 4,
                target=34962,
            )
        ],
        accessors=[
            Accessor(
                bufferView=0,
                byteOffset=0,
                componentType=5126,
                count=3,
                type="VEC3",
            )
        ],
    )
    gltf.set_binary_blob(positions.tobytes())
    path = tmp_path / "invalid-range.glb"
    gltf.save_binary(str(path))

    with pytest.raises(ValueError, match="accessor range exceeds bufferView"):
        parse_glb(str(path))


def test_glb_parser_rejects_indices_outside_position_count(tmp_path):
    from pygltflib import (
        Accessor,
        Asset,
        Attributes,
        Buffer,
        BufferView,
        GLTF2,
        Mesh,
        Node,
        Primitive,
        Scene,
    )

    positions = np.array(
        [[0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [0.0, 1.0, 0.0]],
        dtype=np.float32,
    )
    indices = np.array([0, 1, 3], dtype=np.uint16)
    blob = positions.tobytes() + indices.tobytes()
    gltf = GLTF2(
        asset=Asset(version="2.0"),
        scenes=[Scene(nodes=[0])],
        scene=0,
        nodes=[Node(mesh=0)],
        meshes=[
            Mesh(
                primitives=[
                    Primitive(attributes=Attributes(POSITION=0), indices=1)
                ]
            )
        ],
        buffers=[Buffer(byteLength=len(blob))],
        bufferViews=[
            BufferView(
                buffer=0,
                byteOffset=0,
                byteLength=positions.nbytes,
                target=34962,
            ),
            BufferView(
                buffer=0,
                byteOffset=positions.nbytes,
                byteLength=indices.nbytes,
                target=34963,
            ),
        ],
        accessors=[
            Accessor(
                bufferView=0,
                byteOffset=0,
                componentType=5126,
                count=3,
                type="VEC3",
            ),
            Accessor(
                bufferView=1,
                byteOffset=0,
                componentType=5123,
                count=3,
                type="SCALAR",
            ),
        ],
    )
    gltf.set_binary_blob(blob)
    path = tmp_path / "bad-index.glb"
    gltf.save_binary(str(path))

    with pytest.raises(ValueError, match="outside POSITION count"):
        parse_glb(str(path))
