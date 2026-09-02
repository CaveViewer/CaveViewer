"""Pure geometry and state helpers for routine flat loading indicators."""

from __future__ import annotations


INDETERMINATE_SEGMENT_FRACTION = 0.28
ROUTINE_PROGRESS_BAR_WIDTH = 300.0
ROUTINE_PROGRESS_BAR_HEIGHT = 4.0
ROUTINE_PROGRESS_LABEL_OFFSET = 60.0
OPENGL_PROGRESS_BASE_WINDOW_SIZE = (1536, 864)
OPENGL_PROGRESS_LAYOUT_SCALE_MAX = 1.32
OPENGL_PROGRESS_LABEL_TEXT_SIZE = 2.55
OPENGL_COUNTDOWN_DIAMETER = 172.0
OPENGL_COUNTDOWN_STROKE_WIDTH = ROUTINE_PROGRESS_BAR_HEIGHT
OPENGL_COUNTDOWN_RING_SEGMENTS = 96


def progress_layout_scale(window_size: tuple[int, int]) -> float:
    """Return the shared responsive scale for OpenGL loading feedback."""
    try:
        width, height = (max(1, int(value)) for value in window_size)
    except (TypeError, ValueError):
        width, height = OPENGL_PROGRESS_BASE_WINDOW_SIZE
    base_width, base_height = OPENGL_PROGRESS_BASE_WINDOW_SIZE
    surface_ratio = min(width / base_width, height / base_height)
    return max(1.0, min(OPENGL_PROGRESS_LAYOUT_SCALE_MAX, surface_ratio))


def hex_color_rgb(color: str) -> tuple[float, float, float]:
    """Convert a validated six-digit brand color to normalized RGB values."""
    return tuple(
        int(color[index : index + 2], 16) / 255.0
        for index in (1, 3, 5)
    )


def clamp_progress(value: float) -> float:
    """Return a finite progress value bounded to the inclusive unit range."""
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return 0.0
    if numeric != numeric:  # NaN
        return 0.0
    return max(0.0, min(1.0, numeric))


def monotonic_progress(previous: float, current: float) -> float:
    """Advance determinate progress without allowing visual regression."""
    return max(clamp_progress(previous), clamp_progress(current))


def progress_segments(
    left: float,
    right: float,
    progress: float | None,
    *,
    phase: float = 0.0,
    segment_fraction: float = INDETERMINATE_SEGMENT_FRACTION,
) -> tuple[tuple[float, float], ...]:
    """Return bounded fill segments for determinate or indeterminate bars."""
    width = max(0.0, float(right) - float(left))
    if width == 0.0:
        return ()
    if progress is not None:
        fill_right = float(left) + width * clamp_progress(progress)
        return () if fill_right <= left else ((float(left), fill_right),)

    segment_width = width * clamp_progress(segment_fraction)
    if segment_width == 0.0:
        return ()
    start = (
        float(left)
        + (width + segment_width) * (float(phase) % 1.0)
        - segment_width
    )
    end = start + segment_width
    if end <= left or start >= right:
        return ()
    return ((max(float(left), start), min(float(right), end)),)


def circular_progress_ranges(
    progress: float | None,
    *,
    phase: float = 0.0,
    segment_fraction: float = INDETERMINATE_SEGMENT_FRACTION,
) -> tuple[tuple[float, float], ...]:
    """Return clockwise unit-circle ranges for determinate or moving progress."""
    if progress is not None:
        clamped = clamp_progress(progress)
        return () if clamped == 0.0 else ((0.0, clamped),)

    span = clamp_progress(segment_fraction)
    if span == 0.0:
        return ()
    start = float(phase) % 1.0
    end = start + span
    if end <= 1.0:
        return ((start, end),)
    return ((start, 1.0), (0.0, end - 1.0))
