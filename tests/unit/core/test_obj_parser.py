"""Validate explicit OBJ parser failures for malformed face data."""

from __future__ import annotations

import pytest

from caveviewer.core.mesh.obj import parse_obj


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

    with pytest.raises(ValueError, match="out of range"):
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

    with pytest.raises(ValueError, match="Malformed OBJ face token"):
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

    with pytest.raises(ValueError, match="expected at least 3 vertices"):
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

    with pytest.raises(ValueError, match="Malformed OBJ face token"):
        parse_obj(str(source))
