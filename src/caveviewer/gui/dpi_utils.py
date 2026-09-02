"""Tk DPI-scaling setup from typed presentation profile and native action facade.

Small DPI helpers for Tk-based windows/dialogs.

Without process DPI awareness or explicit Tk scaling, high-DPI displays can
make the entire splash screen look blocky or undersized even when the font
itself is fine.
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass

from caveviewer.gui.platform.presentation import (
    PresentationProfile,
    get_presentation_profile,
)
from caveviewer.gui.platform.presentation_actions import (
    PresentationActionsAdapter,
    WindowMonitorMetrics,
    create_presentation_actions_adapter,
)


_DPI_AWARENESS_CONFIGURED = False
_TK_POINTS_PER_96_DPI = 96.0 / 72.0
_MIN_WINDOWS_LARGE_MONITOR_DENSITY = 0.875


@dataclass(frozen=True, slots=True)
class TkDisplayMetrics:
    """One root's point-font and logical-pixel scaling measurements."""

    tk_point_scale: float
    geometry_scale: float
    density_scale: float
    layout_scale: float
    native_dpi: float | None
    monitor_diagonal_inches: float | None
    override_active: bool
    monitor_id: int | None = None
    work_area: tuple[int, int, int, int] | None = None
    target_tk_point_scale: float | None = None


@dataclass(frozen=True, slots=True)
class TkWindowGeometry:
    """Outer Tk window bounds in native screen pixels."""

    width: int
    height: int
    x: int
    y: int


def configure_process_dpi_awareness(
    *,
    presentation_actions_adapter: PresentationActionsAdapter | None = None,
) -> None:
    """Best-effort Windows process DPI awareness setup.

    Must run before creating Tk roots/windows. Safe no-op on non-Windows
    platforms or when another subsystem already configured DPI awareness.
    """
    global _DPI_AWARENESS_CONFIGURED
    if _DPI_AWARENESS_CONFIGURED:
        return
    _DPI_AWARENESS_CONFIGURED = True
    adapter = presentation_actions_adapter or create_presentation_actions_adapter()
    adapter.configure_process_dpi_awareness()


def apply_tk_scaling(
    root,
    *,
    presentation_profile: PresentationProfile | None = None,
    scale_override: float | None = None,
) -> None:
    """Apply only an explicit Tk scaling override when one is configured.

    Tk initializes point-font scaling from the native desktop before this
    function runs. Reassigning that same derived value is unnecessary on
    Windows and makes the independent geometry conversion look like a second
    scaling pass.
    """
    resolve_tk_display_metrics(
        root,
        presentation_profile=presentation_profile,
        scale_override=scale_override,
    )


def tk_display_scale(
    root,
    *,
    presentation_profile: PresentationProfile | None = None,
    scale_override: float | None = None,
) -> float:
    """Return display scale relative to 96 DPI for pixel-sized Tk layout values."""
    return resolve_tk_display_metrics(
        root,
        presentation_profile=presentation_profile,
        scale_override=scale_override,
    ).geometry_scale


def resolve_tk_display_metrics(
    root,
    *,
    presentation_profile: PresentationProfile | None = None,
    presentation_actions_adapter: PresentationActionsAdapter | None = None,
    scale_override: float | None = None,
) -> TkDisplayMetrics:
    """Resolve native font and geometry scales once for an existing Tk root."""
    profile = presentation_profile or get_presentation_profile()
    tk_point_scale = _current_tk_point_scale(root)
    if not profile.supports_tk_display_scaling:
        return TkDisplayMetrics(
            tk_point_scale=tk_point_scale,
            geometry_scale=1.0,
            density_scale=1.0,
            layout_scale=1.0,
            native_dpi=None,
            monitor_diagonal_inches=None,
            override_active=False,
        )

    explicit_scale = _explicit_tk_scale(scale_override)
    if explicit_scale is not None:
        try:
            root.tk.call("tk", "scaling", explicit_scale)
            tk_point_scale = _current_tk_point_scale(root)
        except Exception:
            tk_point_scale = explicit_scale
        geometry_scale = max(
            0.75,
            min(3.0, explicit_scale / _TK_POINTS_PER_96_DPI),
        )
        return TkDisplayMetrics(
            tk_point_scale=tk_point_scale,
            geometry_scale=geometry_scale,
            density_scale=1.0,
            layout_scale=geometry_scale,
            native_dpi=None,
            monitor_diagonal_inches=None,
            override_active=True,
            target_tk_point_scale=explicit_scale,
        )

    native_dpi = None
    monitor_diagonal_inches = None
    monitor_id = None
    work_area = None
    density_scale = 1.0
    if profile.platform_name == "windows":
        adapter = presentation_actions_adapter or create_presentation_actions_adapter(
            platform_name=profile.platform_name
        )
        try:
            native_dpi = adapter.window_dpi(root)
        except Exception:
            native_dpi = None
        try:
            monitor_metrics = adapter.window_monitor_metrics(root)
        except Exception:
            monitor_metrics = None
        monitor_diagonal_inches = _monitor_diagonal_inches(monitor_metrics)
        if monitor_metrics is not None:
            monitor_id = monitor_metrics.monitor_id
            work_area = monitor_metrics.work_area
        density_scale = _density_scale_for_diagonal(monitor_diagonal_inches)
        if native_dpi is not None and native_dpi > 0:
            geometry_scale = max(1.0, min(3.0, native_dpi / 96.0))
            return TkDisplayMetrics(
                tk_point_scale=tk_point_scale,
                geometry_scale=geometry_scale,
                density_scale=density_scale,
                layout_scale=geometry_scale * density_scale,
                native_dpi=native_dpi,
                monitor_diagonal_inches=monitor_diagonal_inches,
                override_active=False,
                monitor_id=monitor_id,
                work_area=work_area,
                target_tk_point_scale=max(
                    0.75,
                    min(4.0, native_dpi / 72.0),
                ),
            )

    geometry_scale = _tk_geometry_scale_fallback(root)
    return TkDisplayMetrics(
        tk_point_scale=tk_point_scale,
        geometry_scale=geometry_scale,
        density_scale=density_scale,
        layout_scale=geometry_scale * density_scale,
        native_dpi=native_dpi,
        monitor_diagonal_inches=monitor_diagonal_inches,
        override_active=False,
        monitor_id=monitor_id,
        work_area=work_area,
    )


def synchronize_tk_point_scale(root, metrics: TkDisplayMetrics) -> float:
    """Apply and return the destination point scale for retained-root rebuilds."""
    target = metrics.target_tk_point_scale
    if target is None:
        return _current_tk_point_scale(root)
    try:
        root.tk.call("tk", "scaling", target)
    except Exception:
        return _current_tk_point_scale(root)
    return _current_tk_point_scale(root)


def display_scale_changed(
    current: TkDisplayMetrics,
    candidate: TkDisplayMetrics,
    *,
    tolerance: float = 0.005,
) -> bool:
    """Return whether a settled monitor move requires Tk recomposition."""
    if current.override_active or candidate.override_active:
        return False
    return abs(candidate.layout_scale - current.layout_scale) > tolerance


def scale_window_geometry(
    geometry: TkWindowGeometry,
    *,
    current_scale: float,
    candidate_scale: float,
    minimum_size: tuple[int, int],
    preferred_size: tuple[int, int],
    work_area: tuple[int, int, int, int] | None,
) -> TkWindowGeometry:
    """Scale bounds, retain the destination default floor, and clamp them."""
    source = max(0.01, float(current_scale))
    ratio = max(0.01, float(candidate_scale)) / source
    width = max(
        int(minimum_size[0]),
        int(preferred_size[0]),
        int(round(geometry.width * ratio)),
    )
    height = max(
        int(minimum_size[1]),
        int(preferred_size[1]),
        int(round(geometry.height * ratio)),
    )
    x, y = int(geometry.x), int(geometry.y)
    if work_area is not None:
        left, top, right, bottom = (int(value) for value in work_area)
        available_width = max(1, right - left)
        available_height = max(1, bottom - top)
        width = min(width, available_width)
        height = min(height, available_height)
        x = min(max(x, left), right - width)
        y = min(max(y, top), bottom - height)
    return TkWindowGeometry(width=width, height=height, x=x, y=y)


def _monitor_diagonal_inches(metrics: WindowMonitorMetrics | None) -> float | None:
    """Return a plausible physical diagonal from raw monitor measurements."""
    if metrics is None:
        return None
    values = (
        metrics.pixel_width,
        metrics.pixel_height,
        metrics.raw_dpi_x,
        metrics.raw_dpi_y,
    )
    try:
        if not all(math.isfinite(float(value)) for value in values):
            return None
        if metrics.pixel_width <= 0 or metrics.pixel_height <= 0:
            return None
        if not (40.0 <= metrics.raw_dpi_x <= 600.0):
            return None
        if not (40.0 <= metrics.raw_dpi_y <= 600.0):
            return None
        diagonal = math.hypot(
            metrics.pixel_width / metrics.raw_dpi_x,
            metrics.pixel_height / metrics.raw_dpi_y,
        )
    except (TypeError, ValueError, ZeroDivisionError):
        return None
    return diagonal if 5.0 <= diagonal <= 100.0 else None


def _density_scale_for_diagonal(diagonal_inches: float | None) -> float:
    """Return CaveViewer's bounded density adjustment for a monitor diagonal."""
    if diagonal_inches is None:
        return 1.0
    try:
        diagonal = float(diagonal_inches)
    except (TypeError, ValueError):
        return 1.0
    if not math.isfinite(diagonal) or diagonal <= 0:
        return 1.0
    return max(
        _MIN_WINDOWS_LARGE_MONITOR_DENSITY,
        min(1.00, 24.0 / diagonal),
    )


def _current_tk_point_scale(root) -> float:
    """Return Tk's already-initialized pixels-per-point value."""
    try:
        scale = float(root.tk.call("tk", "scaling"))
        if scale > 0:
            return scale
    except Exception:
        pass
    return _TK_POINTS_PER_96_DPI


def _explicit_tk_scale(scale_override: float | None) -> float | None:
    """Return a bounded explicit development override, if present and valid."""
    candidate = scale_override
    if candidate is None:
        environment_value = os.getenv("CAVEVIEWER_TK_SCALE", "").strip()
        if not environment_value:
            return None
        candidate = environment_value
    try:
        return max(0.75, min(4.0, float(candidate)))
    except (TypeError, ValueError):
        return None


def _tk_geometry_scale_fallback(root) -> float:
    """Derive geometry scale from Tk when no native window DPI is available."""
    try:
        pixels_per_inch = float(root.winfo_fpixels("1i"))
        if pixels_per_inch > 0:
            return max(1.0, min(3.0, pixels_per_inch / 96.0))
    except Exception:
        pass
    return 1.0
