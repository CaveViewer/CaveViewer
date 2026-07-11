from __future__ import annotations

import os

import numpy as np
import pytest

from caveviewer.app import find_input_files, find_model_file
from caveviewer.core.glb_parser import parse_glb


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


def test_find_input_files_falls_back_to_available_mtl(tmp_path):
    obj = tmp_path / "cave.obj"
    fallback = tmp_path / "fallback.mtl"
    obj.write_text("mtllib missing.mtl\nv 0 0 0\n", encoding="utf-8")
    fallback.write_text("newmtl rock\n", encoding="utf-8")
    assert find_input_files(str(tmp_path)) == (str(obj), str(fallback))


def test_find_model_returns_glb_descriptor_without_companion_files(tmp_path):
    glb = tmp_path / "cave.glb"
    glb.write_bytes(b"glTF")
    assert find_model_file(str(tmp_path)) == {"format": "glb", "glb_path": str(glb)}


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

    mesh, embedded_images = parse_glb(str(path))

    np.testing.assert_array_equal(mesh.positions, positions)
    np.testing.assert_array_equal(mesh.face_pos_idx, [[0, 1, 2]])
    assert embedded_images == {}
