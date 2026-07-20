"""Validate explicit OBJ parser failures for malformed face data."""

from __future__ import annotations

import re

import pytest

from caveviewer.core.mesh.obj import parse_mtl, parse_obj


def test_parse_obj_rejects_out_of_range_face_index(tmp_path):
    source = tmp_path / "bad.obj"
    source.write_text(
        "\n".join(
            [
                "v 0 0 0",
                "v 1 0 0",
                "v 0 1 0",
                "f 1 2 4",
            ]
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match=r"bad\.obj:4: .*out of range"):
        parse_obj(str(source))


def test_parse_obj_rejects_malformed_face_token(tmp_path):
    source = tmp_path / "bad.obj"
    source.write_text(
        "\n".join(
            [
                "v 0 0 0",
                "v 1 0 0",
                "v 0 1 0",
                "f 1 2 3bad",
            ]
        ),
        encoding="utf-8",
    )

    with pytest.raises(
        ValueError,
        match=r"bad\.obj:4: Malformed OBJ face token",
    ):
        parse_obj(str(source))


def test_parse_obj_rejects_face_with_too_few_vertices(tmp_path):
    source = tmp_path / "bad.obj"
    source.write_text(
        "\n".join(
            [
                "v 0 0 0",
                "v 1 0 0",
                "f 1 2",
            ]
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match=r"bad\.obj:3: .*expected at least 3"):
        parse_obj(str(source))


def test_parse_obj_rejects_incomplete_face_vertex_token(tmp_path):
    source = tmp_path / "bad.obj"
    source.write_text(
        "\n".join(
            [
                "v 0 0 0",
                "v 1 0 0",
                "v 0 1 0",
                "vt 0 0",
                "vt 1 0",
                "vt 0 1",
                "f 1/ 2/2 3/3",
            ]
        ),
        encoding="utf-8",
    )

    with pytest.raises(
        ValueError,
        match=r"bad\.obj:7: Malformed OBJ face token",
    ):
        parse_obj(str(source))


def test_parse_obj_rejects_malformed_vertex_with_line_number(tmp_path):
    source = tmp_path / "bad.obj"
    source.write_text(
        "\n".join(
            [
                "v 0 0 0",
                "v 1 0",
                "v 0 1 0",
                "f 1 2 3",
            ]
        ),
        encoding="utf-8",
    )

    with pytest.raises(
        ValueError,
        match=r"bad\.obj:2: v requires at least 3 numeric",
    ):
        parse_obj(str(source))


def test_parse_obj_rejects_invalid_utf8(tmp_path):
    source = tmp_path / "bad.obj"
    source.write_bytes(b"v 0 0 0\n\xff\n")

    with pytest.raises(
        ValueError,
        match=re.escape(f"OBJ file {source} is not valid UTF-8"),
    ):
        parse_obj(str(source))


def test_parse_mtl_rejects_missing_material_name_with_line_number(tmp_path):
    source = tmp_path / "bad.mtl"
    source.write_text("newmtl \n", encoding="utf-8")

    with pytest.raises(
        ValueError,
        match=r"bad\.mtl:1: newmtl requires an argument",
    ):
        parse_mtl(str(source))


def test_parse_mtl_rejects_texture_before_material_with_line_number(tmp_path):
    source = tmp_path / "bad.mtl"
    source.write_text("map_Kd texture.jpg\n", encoding="utf-8")

    with pytest.raises(
        ValueError,
        match=r"bad\.mtl:1: map_Kd appears before newmtl",
    ):
        parse_mtl(str(source))
