"""Protect startup artwork sizing, fallback, redraw cost, and surface cleanup."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from PIL import Image

from caveviewer.gui import launch_background
from caveviewer.resources import image_path


@pytest.mark.parametrize("size", [(840, 600), (1080, 740), (1620, 1110)])
def test_mesh_covers_viewport_with_quiet_detail_and_clear_text_center(size):
    with Image.open(image_path("startup-mesh.png")) as source:
        rendered = launch_background.render_launch_background(source.convert("L"), size)

    assert rendered.size == size
    assert rendered.mode == "RGB"
    # Keep even the brightest mesh lines subdued; preserve visible detail.
    assert all(hi < 65 and hi > lo for lo, hi in rendered.getextrema())
    center = rendered.getpixel((size[0] // 2, size[1] // 2))
    assert max(center) < 25


def test_background_reuses_image_until_viewport_changes_and_releases_on_destroy(monkeypatch):
    canvas = Mock()
    canvas.create_image.return_value = 42
    photos = []

    def photo(image, *, master):
        assert master is canvas
        result = SimpleNamespace(size=image.size)
        photos.append(result)
        return result

    monkeypatch.setattr(launch_background.ImageTk, "PhotoImage", photo)
    background = launch_background.LaunchBackground(canvas)
    background.resize(840, 600)
    background.resize(840, 600)
    assert len(photos) == 1
    background.resize(1080, 740)
    assert [photo.size for photo in photos] == [(840, 600), (1080, 740)]
    assert canvas.create_image.call_count == 1
    canvas.itemconfigure.assert_called_once_with(42, image=photos[-1])
    canvas.tag_lower.assert_called_with(42)

    destroy = canvas.bind.call_args.args[1]
    destroy()
    background.resize(1080, 740)
    assert background._source is None
    assert background._photo is None
    assert background._canvas is None
    assert len(photos) == 2


@pytest.mark.parametrize("contents", [None, b"invalid PNG"])
def test_unavailable_mesh_preserves_plain_launch_surface(monkeypatch, tmp_path, contents):
    path = tmp_path / "mesh.png"
    if contents is not None:
        path.write_bytes(contents)
    monkeypatch.setattr(launch_background, "image_path", lambda _name: path)
    canvas = Mock()
    background = launch_background.LaunchBackground(canvas)
    background.resize(840, 600)
    canvas.create_image.assert_not_called()
    canvas.bind.call_args.args[1]()


def test_destroyed_canvas_during_photo_creation_does_not_interrupt_startup(monkeypatch):
    monkeypatch.setattr(
        launch_background.ImageTk, "PhotoImage",
        Mock(side_effect=launch_background.tk.TclError("canvas destroyed")),
    )
    canvas = Mock()
    background = launch_background.LaunchBackground(canvas)
    background.resize(840, 600)
    canvas.create_image.assert_not_called()
    assert background._photo is None
    canvas.bind.call_args.args[1]()
