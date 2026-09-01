"""Behavior tests for the screenshot-polish skill helper."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest
from PIL import Image


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = (
    REPOSITORY_ROOT
    / ".agents"
    / "skills"
    / "caveviewer-screenshot-polish"
    / "scripts"
    / "clean_window_capture.py"
)


@pytest.fixture(scope="module")
def screenshot_helper() -> ModuleType:
    """Load the checked-in helper as a module without packaging it."""
    module_name = "caveviewer_screenshot_cleanup_helper"
    specification = importlib.util.spec_from_file_location(module_name, SCRIPT_PATH)
    assert specification is not None
    assert specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    sys.modules[module_name] = module
    specification.loader.exec_module(module)
    return module


def test_clean_capture_trims_only_requested_edges(
    tmp_path: Path,
    screenshot_helper: ModuleType,
) -> None:
    source_path = tmp_path / "captured.png"
    output_path = tmp_path / "clean.png"
    fringe = (91, 82, 73, 255)
    window = (10, 10, 13, 255)
    source = Image.new("RGBA", (12, 10), fringe)
    source.paste(window, (1, 1, 11, 9))
    source.save(source_path)

    report = screenshot_helper.clean_capture(
        source_path,
        output_path,
        trim=screenshot_helper.EdgeInsets(top=1, right=1, bottom=1, left=1),
    )

    with Image.open(output_path) as cleaned:
        assert cleaned.size == (10, 8)
        assert all(
            cleaned.getpixel((x, y)) == window
            for y in range(cleaned.height)
            for x in range(cleaned.width)
        )
    with Image.open(source_path) as unchanged_source:
        assert unchanged_source.size == (12, 10)
        assert unchanged_source.getpixel((0, 0)) == fringe
    assert report.input_size == (12, 10)
    assert report.output_size == (10, 8)


def test_clean_capture_refuses_destructive_output_by_default(
    tmp_path: Path,
    screenshot_helper: ModuleType,
) -> None:
    source_path = tmp_path / "captured.png"
    output_path = tmp_path / "existing.png"
    Image.new("RGBA", (8, 8), (10, 10, 13, 255)).save(source_path)
    Image.new("RGBA", (2, 2), (255, 0, 0, 255)).save(output_path)

    with pytest.raises(FileExistsError):
        screenshot_helper.clean_capture(source_path, output_path)
    with pytest.raises(ValueError, match="must differ"):
        screenshot_helper.clean_capture(source_path, source_path, replace=True)

    with Image.open(output_path) as existing:
        assert existing.size == (2, 2)
        assert existing.getpixel((0, 0)) == (255, 0, 0, 255)


def test_bottom_corner_mask_preserves_every_pixel_outside_corner_boxes(
    screenshot_helper: ModuleType,
) -> None:
    window = (10, 10, 13, 255)
    flattened_background = (63, 64, 65, 255)
    source = Image.new("RGBA", (20, 20), window)
    for y in range(16, 20):
        for x in (*range(0, 4), *range(16, 20)):
            source.putpixel((x, y), flattened_background)

    cleaned, report = screenshot_helper.clean_image(
        source,
        corner_radius=4,
        corners="bottom",
        edge_color=window[:3],
        supersample=8,
    )

    assert cleaned.getpixel((0, 19)) == (*window[:3], 0)
    assert cleaned.getpixel((10, 19)) == window
    assert report.transparent_corner_pixels > 0
    assert report.partial_corner_pixels > 0
    for y in range(cleaned.height):
        for x in range(cleaned.width):
            in_corner_box = y >= 16 and (x < 4 or x >= 16)
            if not in_corner_box:
                assert cleaned.getpixel((x, y)) == source.getpixel((x, y))

    cleaned.close()
    source.close()
