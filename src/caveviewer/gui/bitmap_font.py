"""Bitmap-font rasterization for OpenGL overlay text.

FreeType-based text rasterization helpers for the overlay UI.

Public API is intentionally stable so existing UI modules do not need to
change call sites:
- text_width_px(text, pixel_size, letter_spacing=0.0)
- text_height_px(pixel_size)
- text_bounds_px(text, pixel_size, letter_spacing=0.0)
- iter_text_pixels(text, origin_x, origin_y, pixel_size, letter_spacing=0.0)
- pixel_size_at_text_scale(pixel_size, target_scale)
- set_presentation_profile(profile), set_raster_scale(scale) / raster_scale()

iter_text_pixels yields tuples:
(px_x0, px_y0, px_x1, px_y1, alpha)
where alpha is in [0, 1].
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache

import freetype

from caveviewer.gui.platform.presentation import (
    PresentationProfile,
    font_candidates_for_profile,
    get_presentation_profile,
)


_BASE_GRID_HEIGHT = 8.5
_TEXT_SCALE = 1.0
_RASTER_SCALE = 1.0
_PRESENTATION_PROFILE: PresentationProfile | None = None
_RUNTIME_STYLE_ACTIVE = False
_RUNTIME_FONT_PATH: str | None = None
_RUNTIME_AA_MODE: str | None = None


def set_presentation_profile(profile: PresentationProfile | None) -> None:
    """Set or clear the process-selected profile used for overlay font rendering."""
    global _PRESENTATION_PROFILE
    if profile == _PRESENTATION_PROFILE:
        return
    _PRESENTATION_PROFILE = profile
    _resolve_font_path.cache_clear()
    _load_face.cache_clear()
    _glyph_for.cache_clear()


def configure_runtime_style(
    *,
    font_path: str | None,
    antialiasing_mode: str,
) -> None:
    """Use one viewer-owned style snapshot instead of process environment."""

    global _RUNTIME_STYLE_ACTIVE, _RUNTIME_FONT_PATH, _RUNTIME_AA_MODE
    _RUNTIME_STYLE_ACTIVE = True
    _RUNTIME_FONT_PATH = font_path
    _RUNTIME_AA_MODE = antialiasing_mode
    _resolve_font_path.cache_clear()
    _load_face.cache_clear()
    _glyph_for.cache_clear()


def clear_runtime_style() -> None:
    """Return direct legacy callers to their environment-backed fallback."""

    global _RUNTIME_STYLE_ACTIVE, _RUNTIME_FONT_PATH, _RUNTIME_AA_MODE
    if not _RUNTIME_STYLE_ACTIVE and _RUNTIME_FONT_PATH is None and _RUNTIME_AA_MODE is None:
        return
    _RUNTIME_STYLE_ACTIVE = False
    _RUNTIME_FONT_PATH = None
    _RUNTIME_AA_MODE = None
    _resolve_font_path.cache_clear()
    _load_face.cache_clear()
    _glyph_for.cache_clear()


def _active_presentation_profile() -> PresentationProfile:
    """Return the runtime-selected profile or the pure direct-call fallback."""
    return _PRESENTATION_PROFILE or get_presentation_profile()


def set_text_scale(scale: float) -> None:
    """Set global UI text scale and clear font caches when it changes."""
    global _TEXT_SCALE
    clamped = max(0.5, min(3.0, float(scale)))
    if abs(clamped - _TEXT_SCALE) < 1e-6:
        return
    _TEXT_SCALE = clamped
    _load_face.cache_clear()
    _glyph_for.cache_clear()


def set_raster_scale(scale: float) -> None:
    """Set framebuffer raster scale while preserving logical UI layout size."""
    global _RASTER_SCALE
    clamped = max(1.0, min(4.0, float(scale)))
    if abs(clamped - _RASTER_SCALE) < 1e-6:
        return
    _RASTER_SCALE = clamped
    _load_face.cache_clear()
    _glyph_for.cache_clear()


def raster_scale() -> float:
    """Return the active framebuffer raster scale used by generated glyphs."""
    return _RASTER_SCALE


def pixel_size_at_text_scale(pixel_size: float, target_scale: float) -> float:
    """Return a pixel-size argument that renders at ``target_scale``.

    Fixed-geometry controls use this to retain their designed text dimensions
    while other overlays follow the global UI text scale.
    """
    return float(pixel_size) * float(target_scale) / _TEXT_SCALE


@dataclass(frozen=True)
class _Glyph:
    index: int
    left: float
    top: float
    width: float
    rows: float
    advance: float
    pixels: tuple[tuple[float, float, float], ...]


def _font_pixel_height(pixel_size: float) -> int:
    # Preserve legacy sizing intent: previous bitmap font height was ~7*pixel_size.
    return max(8, int(round(pixel_size * _BASE_GRID_HEIGHT * _TEXT_SCALE * _RASTER_SCALE)))


def _font_candidates() -> list[str]:
    candidates: list[str] = []
    if _RUNTIME_STYLE_ACTIVE:
        if _RUNTIME_FONT_PATH:
            candidates.append(_RUNTIME_FONT_PATH)
    else:
        env_font = os.getenv("CAVEVIEWER_UI_FONT")
        if env_font:
            candidates.append(env_font)

    # The profile is pure; fontconfig lookup remains an action-time fallback.
    candidates.extend(font_candidates_for_profile(_active_presentation_profile()))

    return candidates


@lru_cache(maxsize=32)
def _resolve_font_path() -> str:
    for candidate in _font_candidates():
        if os.path.exists(candidate):
            return candidate
    raise RuntimeError(
        "No usable UI font found for FreeType renderer. "
        "Set CAVEVIEWER_UI_FONT to a .ttf/.otf/.ttc path."
    )


def _get_aa_target() -> int:
    """Get FreeType anti-aliasing target mode from environment or default.

    CAVEVIEWER_TEXT_AA_MODE can be:
    - 'lcd': LCD sub-pixel rendering (sharper on Retina/high-DPI, uses more memory)
    - 'light': Light auto-hinting (smooth curves, matches macOS CoreText style)
    - 'normal': Standard grid-fitted anti-aliasing

    macOS and Linux default to 'light' because it keeps CaveViewer's
    FreeType-rendered overlay text smooth on high-DPI and fractional-scale
    desktops without requiring users to set environment variables.
    """
    env = (
        _RUNTIME_AA_MODE
        if _RUNTIME_STYLE_ACTIVE
        else os.getenv("CAVEVIEWER_TEXT_AA_MODE", "").lower()
    )
    if not env:
        mode = _active_presentation_profile().default_text_antialiasing_mode
    else:
        mode = env
    if mode == "lcd":
        return freetype.FT_LOAD_TARGET_LCD
    elif mode == "light":
        return freetype.FT_LOAD_TARGET_LIGHT | freetype.FT_LOAD_FORCE_AUTOHINT
    else:
        return freetype.FT_LOAD_TARGET_NORMAL


@lru_cache(maxsize=64)
def _load_face(font_px: int) -> freetype.Face:
    face = freetype.Face(_resolve_font_path())
    face.set_pixel_sizes(0, font_px)
    return face


def _line_metrics(face: freetype.Face) -> tuple[float, float, float]:
    # 26.6 fixed-point -> pixels
    asc = float(face.size.ascender) / 64.0 / _RASTER_SCALE
    desc = float(face.size.descender) / 64.0 / _RASTER_SCALE
    height = float(face.size.height) / 64.0 / _RASTER_SCALE
    if height <= 0.0:
        height = asc - desc
    return asc, desc, height


@lru_cache(maxsize=4096)
def _glyph_for(font_px: int, ch: str) -> _Glyph:
    face = _load_face(font_px)
    idx = face.get_char_index(ch)
    if idx == 0:
        # Missing glyph: advance by roughly one-third of em to keep layout stable.
        fallback_advance = max(1.0, font_px * 0.35 / _RASTER_SCALE)
        return _Glyph(index=0, left=0, top=0, width=0, rows=0, advance=fallback_advance, pixels=tuple())

    aa_target = _get_aa_target()
    face.load_glyph(idx, freetype.FT_LOAD_RENDER | aa_target)
    slot = face.glyph
    bmp = slot.bitmap

    width = int(bmp.width)
    rows = int(bmp.rows)
    left = float(slot.bitmap_left) / _RASTER_SCALE
    top = float(slot.bitmap_top) / _RASTER_SCALE
    advance = float(slot.advance.x) / 64.0 / _RASTER_SCALE

    pts: list[tuple[float, float, float]] = []
    if width > 0 and rows > 0:
        buf = bmp.buffer
        pitch = abs(int(bmp.pitch))
        for y in range(rows):
            row_off = y * pitch
            for x in range(width):
                a = buf[row_off + x]
                if a:
                    pts.append((x / _RASTER_SCALE, y / _RASTER_SCALE, float(a) / 255.0))

    if advance <= 0.0:
        advance = float(max(width, 1)) / _RASTER_SCALE

    return _Glyph(
        index=idx,
        left=left,
        top=top,
        width=width / _RASTER_SCALE,
        rows=rows / _RASTER_SCALE,
        advance=advance,
        pixels=tuple(pts),
    )


def _kerning_px(face: freetype.Face, left_idx: int, right_idx: int) -> float:
    if left_idx == 0 or right_idx == 0:
        return 0.0
    if not face.has_kerning:
        return 0.0
    k = face.get_kerning(left_idx, right_idx, freetype.FT_KERNING_DEFAULT)
    return float(k.x) / 64.0 / _RASTER_SCALE


def text_width_px(text: str, pixel_size: float, letter_spacing: float = 0.0) -> float:
    """Return rendered text width in screen pixels."""
    if not text:
        return 0.0

    font_px = _font_pixel_height(pixel_size)
    face = _load_face(font_px)
    spacing_px = letter_spacing * pixel_size

    cursor_x = 0.0
    prev_idx = 0
    for i, ch in enumerate(text):
        glyph = _glyph_for(font_px, ch)
        cursor_x += _kerning_px(face, prev_idx, glyph.index)
        cursor_x += glyph.advance
        if i < len(text) - 1:
            cursor_x += spacing_px
        prev_idx = glyph.index
    return cursor_x


def text_height_px(pixel_size: float) -> float:
    """Return typographic line height in screen pixels for this text size."""
    font_px = _font_pixel_height(pixel_size)
    face = _load_face(font_px)
    _asc, _desc, line_h = _line_metrics(face)
    return max(1.0, line_h)


def text_bounds_px(text: str, pixel_size: float, letter_spacing: float = 0.0) -> tuple[float, float, float, float]:
    """Return tight drawn bounds (min_x, min_y, max_x, max_y) for text at origin (0,0)."""
    if not text:
        return (0.0, 0.0, 0.0, 0.0)

    font_px = _font_pixel_height(pixel_size)
    face = _load_face(font_px)
    asc, _desc, _line_h = _line_metrics(face)
    baseline_y = asc
    spacing_px = letter_spacing * pixel_size
    pixel_unit = 1.0 / _RASTER_SCALE

    min_x = float("inf")
    min_y = float("inf")
    max_x = float("-inf")
    max_y = float("-inf")

    cursor_x = 0.0
    prev_idx = 0
    for i, ch in enumerate(text):
        glyph = _glyph_for(font_px, ch)
        cursor_x += _kerning_px(face, prev_idx, glyph.index)

        gx0 = cursor_x + glyph.left
        gy0 = baseline_y - glyph.top

        for x, y, _a in glyph.pixels:
            fx = gx0 + x
            fy = gy0 + y
            if fx < min_x:
                min_x = fx
            if fy < min_y:
                min_y = fy
            if fx + pixel_unit > max_x:
                max_x = fx + pixel_unit
            if fy + pixel_unit > max_y:
                max_y = fy + pixel_unit

        cursor_x += glyph.advance
        if i < len(text) - 1:
            cursor_x += spacing_px
        prev_idx = glyph.index

    if max_x == float("-inf"):
        return (0.0, 0.0, 0.0, 0.0)
    return (min_x, min_y, max_x, max_y)


def iter_text_pixels(text: str, origin_x: float, origin_y: float, pixel_size: float,
                     letter_spacing: float = 0.0):
    """
    Yield alpha-aware pixel quads for rasterized text.

    Yields tuples: (px_x0, px_y0, px_x1, px_y1, alpha)
    where alpha is in [0, 1].
    """
    if not text:
        return

    font_px = _font_pixel_height(pixel_size)
    face = _load_face(font_px)
    asc, _desc, _line_h = _line_metrics(face)
    baseline_y = origin_y + asc
    spacing_px = letter_spacing * pixel_size
    pixel_unit = 1.0 / _RASTER_SCALE

    cursor_x = origin_x
    prev_idx = 0
    for i, ch in enumerate(text):
        glyph = _glyph_for(font_px, ch)
        cursor_x += _kerning_px(face, prev_idx, glyph.index)

        gx0 = cursor_x + glyph.left
        gy0 = baseline_y - glyph.top

        for x, y, a in glyph.pixels:
            px_x0 = gx0 + x
            px_y0 = gy0 + y
            yield (px_x0, px_y0, px_x0 + pixel_unit, px_y0 + pixel_unit, a)

        cursor_x += glyph.advance
        if i < len(text) - 1:
            cursor_x += spacing_px
        prev_idx = glyph.index
