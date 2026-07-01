"""
gui/bitmap_font.py

FreeType-based text rasterization helpers for the overlay UI.

Public API is intentionally stable so existing UI modules do not need to
change call sites:
- text_width_px(text, pixel_size, letter_spacing=0.0)
- text_height_px(pixel_size)
- text_bounds_px(text, pixel_size, letter_spacing=0.0)
- iter_text_pixels(text, origin_x, origin_y, pixel_size, letter_spacing=0.0)

iter_text_pixels yields tuples:
(px_x0, px_y0, px_x1, px_y1, alpha)
where alpha is in [0, 1].
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache

import freetype

from gui.platform.factory import get_platform_adapter


_BASE_GRID_HEIGHT = 8.5
_TEXT_SCALE = 1.0


def set_text_scale(scale: float) -> None:
    """Set global UI text scale and clear font caches when it changes."""
    global _TEXT_SCALE
    clamped = max(0.5, min(3.0, float(scale)))
    if abs(clamped - _TEXT_SCALE) < 1e-6:
        return
    _TEXT_SCALE = clamped
    _load_face.cache_clear()
    _glyph_for.cache_clear()


@dataclass(frozen=True)
class _Glyph:
    index: int
    left: int
    top: int
    width: int
    rows: int
    advance: float
    pixels: tuple[tuple[int, int, float], ...]


def _font_pixel_height(pixel_size: float) -> int:
    # Preserve legacy sizing intent: previous bitmap font height was ~7*pixel_size.
    return max(8, int(round(pixel_size * _BASE_GRID_HEIGHT * _TEXT_SCALE)))


def _font_candidates() -> list[str]:
    env_font = os.getenv("CAVEVIEWER_UI_FONT")
    candidates: list[str] = []
    if env_font:
        candidates.append(env_font)

    # Use platform-specific font candidates from adapter
    adapter = get_platform_adapter()
    candidates.extend(adapter.font_candidates())
    
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
    - 'light': Light hinting (balanced quality/sharpness)
    - 'normal': Standard anti-aliasing (default)
    """
    mode = os.getenv("CAVEVIEWER_TEXT_AA_MODE", "normal").lower()
    if mode == "lcd":
        return freetype.FT_LOAD_TARGET_LCD
    elif mode == "light":
        return freetype.FT_LOAD_TARGET_LIGHT
    else:
        return freetype.FT_LOAD_TARGET_NORMAL


@lru_cache(maxsize=64)
def _load_face(font_px: int) -> freetype.Face:
    face = freetype.Face(_resolve_font_path())
    face.set_pixel_sizes(0, font_px)
    return face


def _line_metrics(face: freetype.Face) -> tuple[float, float, float]:
    # 26.6 fixed-point -> pixels
    asc = float(face.size.ascender) / 64.0
    desc = float(face.size.descender) / 64.0
    height = float(face.size.height) / 64.0
    if height <= 0.0:
        height = asc - desc
    return asc, desc, height


@lru_cache(maxsize=4096)
def _glyph_for(font_px: int, ch: str) -> _Glyph:
    face = _load_face(font_px)
    idx = face.get_char_index(ch)
    if idx == 0:
        # Missing glyph: advance by roughly one-third of em to keep layout stable.
        fallback_advance = max(1.0, font_px * 0.35)
        return _Glyph(index=0, left=0, top=0, width=0, rows=0, advance=fallback_advance, pixels=tuple())

    aa_target = _get_aa_target()
    face.load_glyph(idx, freetype.FT_LOAD_RENDER | aa_target)
    slot = face.glyph
    bmp = slot.bitmap

    width = int(bmp.width)
    rows = int(bmp.rows)
    left = int(slot.bitmap_left)
    top = int(slot.bitmap_top)
    advance = float(slot.advance.x) / 64.0

    pts: list[tuple[int, int, float]] = []
    if width > 0 and rows > 0:
        buf = bmp.buffer
        pitch = abs(int(bmp.pitch))
        for y in range(rows):
            row_off = y * pitch
            for x in range(width):
                a = buf[row_off + x]
                if a:
                    pts.append((x, y, float(a) / 255.0))

    if advance <= 0.0:
        advance = float(max(width, 1))

    return _Glyph(
        index=idx,
        left=left,
        top=top,
        width=width,
        rows=rows,
        advance=advance,
        pixels=tuple(pts),
    )


def _kerning_px(face: freetype.Face, left_idx: int, right_idx: int) -> float:
    if left_idx == 0 or right_idx == 0:
        return 0.0
    if not face.has_kerning:
        return 0.0
    k = face.get_kerning(left_idx, right_idx, freetype.FT_KERNING_DEFAULT)
    return float(k.x) / 64.0


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
            if fx + 1.0 > max_x:
                max_x = fx + 1.0
            if fy + 1.0 > max_y:
                max_y = fy + 1.0

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
            yield (px_x0, px_y0, px_x0 + 1.0, px_y0 + 1.0, a)

        cursor_x += glyph.advance
        if i < len(text) - 1:
            cursor_x += spacing_px
        prev_idx = glyph.index
