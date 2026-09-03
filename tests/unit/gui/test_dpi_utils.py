"""Verify native Tk point scaling and logical-pixel geometry stay independent."""

from __future__ import annotations

import inspect
from types import SimpleNamespace

import pytest

from caveviewer.gui import dpi_utils
from caveviewer.gui import splash_screen
from caveviewer.gui.platform.presentation import select_presentation_profile
from caveviewer.gui.platform.presentation_actions import (
    DefaultPresentationActionsAdapter,
    WindowMonitorMetrics,
    WindowsPresentationActionsAdapter,
)
from caveviewer.gui.dpi_utils import TkWindowGeometry


class _FakeTk:
    def __init__(self, scale: float) -> None:
        self.scale = scale
        self.calls: list[tuple[object, ...]] = []

    def call(self, *arguments):
        self.calls.append(arguments)
        if arguments == ("tk", "scaling"):
            return self.scale
        if arguments[:2] == ("tk", "scaling") and len(arguments) == 3:
            self.scale = float(arguments[2])
            return None
        raise AssertionError(f"Unexpected Tk call: {arguments!r}")


class _FakeRoot:
    def __init__(self, *, tk_scale: float, pixels_per_inch: float) -> None:
        self.tk = _FakeTk(tk_scale)
        self.pixels_per_inch = pixels_per_inch

    def winfo_fpixels(self, value: str) -> float:
        assert value == "1i"
        return self.pixels_per_inch

    def winfo_id(self) -> int:
        return 321


class _NativeDpiAdapter(DefaultPresentationActionsAdapter):
    def __init__(
        self,
        dpi: float | None,
        monitor_metrics: WindowMonitorMetrics | None = None,
    ) -> None:
        self.dpi = dpi
        self.monitor_metrics = monitor_metrics
        self.window_calls = []
        self.monitor_calls = []

    def window_dpi(self, window) -> float | None:
        self.window_calls.append(window)
        return self.dpi

    def window_monitor_metrics(self, window) -> WindowMonitorMetrics | None:
        self.monitor_calls.append(window)
        return self.monitor_metrics


@pytest.mark.parametrize("dpi", (96.0, 144.0, 192.0, 240.0))
def test_windows_native_dpi_scales_geometry_once_without_rewriting_tk(dpi):
    root = _FakeRoot(tk_scale=dpi / 72.0, pixels_per_inch=dpi)
    adapter = _NativeDpiAdapter(dpi)

    metrics = dpi_utils.resolve_tk_display_metrics(
        root,
        presentation_profile=select_presentation_profile(platform_name="win32"),
        presentation_actions_adapter=adapter,
    )

    assert metrics.tk_point_scale == pytest.approx(dpi / 72.0)
    assert metrics.geometry_scale == pytest.approx(dpi / 96.0)
    assert metrics.density_scale == 1.0
    assert metrics.layout_scale == pytest.approx(dpi / 96.0)
    assert metrics.native_dpi == dpi
    assert metrics.monitor_diagonal_inches is None
    assert metrics.target_tk_point_scale == pytest.approx(dpi / 72.0)
    assert not metrics.override_active
    assert adapter.window_calls == [root]
    assert adapter.monitor_calls == [root]
    assert root.tk.calls == [("tk", "scaling")]


def test_explicit_development_override_is_the_only_windows_tk_write():
    root = _FakeRoot(tk_scale=1.0, pixels_per_inch=96.0)
    adapter = _NativeDpiAdapter(240.0)

    metrics = dpi_utils.resolve_tk_display_metrics(
        root,
        presentation_profile=select_presentation_profile(platform_name="win32"),
        presentation_actions_adapter=adapter,
        scale_override=10.0 / 3.0,
    )

    assert metrics.tk_point_scale == pytest.approx(10.0 / 3.0)
    assert metrics.geometry_scale == pytest.approx(2.5)
    assert metrics.density_scale == 1.0
    assert metrics.layout_scale == pytest.approx(2.5)
    assert metrics.native_dpi is None
    assert metrics.override_active
    assert metrics.target_tk_point_scale == pytest.approx(10.0 / 3.0)
    assert adapter.window_calls == []
    assert adapter.monitor_calls == []
    assert root.tk.calls == [
        ("tk", "scaling"),
        ("tk", "scaling", 10.0 / 3.0),
        ("tk", "scaling"),
    ]


def test_environment_override_bypasses_adaptive_monitor_density(monkeypatch):
    monkeypatch.setenv("CAVEVIEWER_TK_SCALE", str(10.0 / 3.0))
    root = _FakeRoot(tk_scale=1.0, pixels_per_inch=240.0)
    adapter = _NativeDpiAdapter(
        240.0,
        WindowMonitorMetrics(2560, 1920, 100.0, 100.0),
    )

    metrics = dpi_utils.resolve_tk_display_metrics(
        root,
        presentation_profile=select_presentation_profile(platform_name="win32"),
        presentation_actions_adapter=adapter,
    )

    assert metrics.override_active
    assert metrics.density_scale == 1.0
    assert metrics.monitor_diagonal_inches is None
    assert adapter.window_calls == []
    assert adapter.monitor_calls == []


def test_windows_geometry_falls_back_to_tk_when_native_dpi_is_unavailable():
    root = _FakeRoot(tk_scale=2.0, pixels_per_inch=144.0)

    metrics = dpi_utils.resolve_tk_display_metrics(
        root,
        presentation_profile=select_presentation_profile(platform_name="win32"),
        presentation_actions_adapter=_NativeDpiAdapter(None),
    )

    assert metrics.geometry_scale == pytest.approx(1.5)
    assert metrics.density_scale == 1.0
    assert metrics.layout_scale == pytest.approx(1.5)
    assert metrics.native_dpi is None
    assert metrics.target_tk_point_scale is None
    assert root.tk.calls == [("tk", "scaling")]


def test_retained_root_synchronizes_point_fonts_to_destination_native_dpi():
    root = _FakeRoot(tk_scale=10.0 / 3.0, pixels_per_inch=240.0)
    metrics = dpi_utils.resolve_tk_display_metrics(
        root,
        presentation_profile=select_presentation_profile(platform_name="win32"),
        presentation_actions_adapter=_NativeDpiAdapter(144.0),
    )

    applied = dpi_utils.synchronize_tk_point_scale(root, metrics)

    assert metrics.tk_point_scale == pytest.approx(10.0 / 3.0)
    assert metrics.target_tk_point_scale == pytest.approx(2.0)
    assert applied == pytest.approx(2.0)
    assert root.tk.scale == pytest.approx(2.0)
    assert root.tk.calls == [
        ("tk", "scaling"),
        ("tk", "scaling", 2.0),
        ("tk", "scaling"),
    ]


def test_point_scale_synchronization_is_a_noop_without_a_native_target():
    root = _FakeRoot(tk_scale=2.0, pixels_per_inch=144.0)
    metrics = dpi_utils.resolve_tk_display_metrics(
        root,
        presentation_profile=select_presentation_profile(platform_name="win32"),
        presentation_actions_adapter=_NativeDpiAdapter(None),
    )
    root.tk.calls.clear()

    applied = dpi_utils.synchronize_tk_point_scale(root, metrics)

    assert applied == pytest.approx(2.0)
    assert root.tk.calls == [("tk", "scaling")]


def test_platform_without_tk_display_scaling_keeps_logical_geometry():
    root = _FakeRoot(tk_scale=2.0, pixels_per_inch=192.0)

    metrics = dpi_utils.resolve_tk_display_metrics(
        root,
        presentation_profile=select_presentation_profile(platform_name="darwin"),
    )

    assert metrics.geometry_scale == 1.0
    assert metrics.density_scale == 1.0
    assert metrics.layout_scale == 1.0
    assert metrics.native_dpi is None
    assert not metrics.override_active


@pytest.mark.parametrize(
    ("diagonal_inches", "expected_density"),
    [
        (16.0, 1.0),
        (24.0, 1.0),
        (25.0, 24.0 / 25.0),
        (26.0, 0.95),
        (32.0, 0.95),
    ],
)
def test_windows_monitor_diagonal_applies_bounded_density(
    diagonal_inches,
    expected_density,
):
    metrics = WindowMonitorMetrics(
        pixel_width=int(diagonal_inches * 80),
        pixel_height=int(diagonal_inches * 60),
        raw_dpi_x=100.0,
        raw_dpi_y=100.0,
    )
    adapter = _NativeDpiAdapter(240.0, metrics)

    display = dpi_utils.resolve_tk_display_metrics(
        _FakeRoot(tk_scale=10.0 / 3.0, pixels_per_inch=240.0),
        presentation_profile=select_presentation_profile(platform_name="win32"),
        presentation_actions_adapter=adapter,
    )

    assert display.monitor_diagonal_inches == pytest.approx(diagonal_inches)
    assert display.density_scale == pytest.approx(expected_density)
    assert display.layout_scale == pytest.approx(2.5 * expected_density)


@pytest.mark.parametrize(
    "monitor_metrics",
    [
        None,
        WindowMonitorMetrics(0, 1440, 100.0, 100.0),
        WindowMonitorMetrics(1920, 1440, 0.0, 100.0),
        WindowMonitorMetrics(1920, 1440, 20.0, 100.0),
        WindowMonitorMetrics(1920, 1440, float("nan"), 100.0),
    ],
)
def test_invalid_monitor_measurements_preserve_native_density(monitor_metrics):
    display = dpi_utils.resolve_tk_display_metrics(
        _FakeRoot(tk_scale=10.0 / 3.0, pixels_per_inch=240.0),
        presentation_profile=select_presentation_profile(platform_name="win32"),
        presentation_actions_adapter=_NativeDpiAdapter(240.0, monitor_metrics),
    )

    assert display.monitor_diagonal_inches is None
    assert display.density_scale == 1.0
    assert display.layout_scale == pytest.approx(2.5)


def test_32_inch_250_percent_shell_contract():
    profile = select_presentation_profile(platform_name="win32")
    display = dpi_utils.resolve_tk_display_metrics(
        _FakeRoot(tk_scale=10.0 / 3.0, pixels_per_inch=240.0),
        presentation_profile=profile,
        presentation_actions_adapter=_NativeDpiAdapter(
            240.0,
            WindowMonitorMetrics(2560, 1920, 100.0, 100.0),
        ),
    )

    assert display.geometry_scale == pytest.approx(2.5)
    assert display.density_scale == pytest.approx(0.95)
    assert display.layout_scale == pytest.approx(2.375)
    assert round(profile.splash_layout.window_width * display.layout_scale) == 2470
    assert round(profile.splash_layout.min_height * display.layout_scale) == 1758
    assert round(profile.splash_layout.resize_min_width * display.layout_scale) == 1995
    assert round(profile.splash_layout.resize_min_height * display.layout_scale) == 1425


def test_observed_31_7_inch_144_dpi_desktop_contract():
    profile = select_presentation_profile(platform_name="win32")
    display = dpi_utils.resolve_tk_display_metrics(
        _FakeRoot(tk_scale=2.0, pixels_per_inch=144.0),
        presentation_profile=profile,
        presentation_actions_adapter=_NativeDpiAdapter(
            144.0,
            WindowMonitorMetrics(2536, 1902, 100.0, 100.0),
        ),
    )

    assert display.monitor_diagonal_inches == pytest.approx(31.7)
    assert display.geometry_scale == pytest.approx(1.5)
    assert display.density_scale == pytest.approx(0.95)
    assert display.layout_scale == pytest.approx(1.425)
    assert round(profile.splash_layout.window_width * display.layout_scale) == 1482
    assert round(profile.splash_layout.min_height * display.layout_scale) == 1054


def test_each_metrics_resolution_remeasures_the_windows_monitor():
    adapter = _NativeDpiAdapter(
        240.0,
        WindowMonitorMetrics(1920, 1440, 100.0, 100.0),
    )
    root = _FakeRoot(tk_scale=10.0 / 3.0, pixels_per_inch=240.0)
    profile = select_presentation_profile(platform_name="win32")

    laptop_display = dpi_utils.resolve_tk_display_metrics(
        root,
        presentation_profile=profile,
        presentation_actions_adapter=adapter,
    )
    adapter.monitor_metrics = WindowMonitorMetrics(2560, 1920, 100.0, 100.0)
    desktop_display = dpi_utils.resolve_tk_display_metrics(
        root,
        presentation_profile=profile,
        presentation_actions_adapter=adapter,
    )

    assert laptop_display.density_scale == 1.0
    assert desktop_display.density_scale == pytest.approx(0.95)
    assert adapter.monitor_calls == [root, root]


def test_monitor_transition_detects_only_a_material_layout_scale_change():
    root = _FakeRoot(tk_scale=10.0 / 3.0, pixels_per_inch=240.0)
    profile = select_presentation_profile(platform_name="win32")
    laptop = dpi_utils.resolve_tk_display_metrics(
        root,
        presentation_profile=profile,
        presentation_actions_adapter=_NativeDpiAdapter(
            240.0,
            WindowMonitorMetrics(1920, 1440, 100.0, 100.0, monitor_id=1),
        ),
    )
    desktop = dpi_utils.resolve_tk_display_metrics(
        root,
        presentation_profile=profile,
        presentation_actions_adapter=_NativeDpiAdapter(
            240.0,
            WindowMonitorMetrics(2560, 1920, 100.0, 100.0, monitor_id=2),
        ),
    )

    assert dpi_utils.display_scale_changed(laptop, desktop)
    assert not dpi_utils.display_scale_changed(laptop, laptop)


def test_window_geometry_scales_by_monitor_ratio_and_clamps_to_work_area():
    scaled = dpi_utils.scale_window_geometry(
        TkWindowGeometry(width=2600, height=1800, x=100, y=80),
        current_scale=2.5,
        candidate_scale=2.0,
        minimum_size=(1680, 1200),
        preferred_size=(2080, 1480),
        work_area=(1920, 0, 3840, 1400),
        destination_position=(3000, 120),
    )

    assert scaled == TkWindowGeometry(
        width=1920,
        height=1400,
        x=1920,
        y=0,
    )


def test_window_geometry_keeps_destination_default_floor_after_scale_drop():
    scaled = dpi_utils.scale_window_geometry(
        TkWindowGeometry(width=2110, height=1500, x=2400, y=100),
        current_scale=2.5,
        candidate_scale=1.2,
        minimum_size=(1008, 720),
        preferred_size=(1248, 888),
        work_area=(1920, 0, 3840, 2160),
    )

    assert scaled == TkWindowGeometry(
        width=1248,
        height=888,
        x=2400,
        y=100,
    )


def test_window_geometry_preserves_larger_user_size_after_scale_change():
    scaled = dpi_utils.scale_window_geometry(
        TkWindowGeometry(width=3200, height=2200, x=2200, y=80),
        current_scale=2.5,
        candidate_scale=1.2,
        minimum_size=(1008, 720),
        preferred_size=(1248, 888),
        work_area=(1920, 0, 3840, 2160),
    )

    assert scaled == TkWindowGeometry(
        width=1536,
        height=1056,
        x=2200,
        y=80,
    )


def test_window_geometry_uses_destination_position_without_observed_size():
    scaled = dpi_utils.scale_window_geometry(
        TkWindowGeometry(width=1482, height=1054, x=2200, y=80),
        current_scale=1.425,
        candidate_scale=2.5,
        minimum_size=(2100, 1500),
        preferred_size=(2600, 1850),
        work_area=(0, 0, 3840, 2160),
        destination_position=(300, 140),
    )

    assert scaled == TkWindowGeometry(
        width=2600,
        height=1850,
        x=300,
        y=140,
    )


def test_window_geometry_round_trip_converts_each_settled_source_once():
    laptop = TkWindowGeometry(width=2600, height=1850, x=200, y=100)
    desktop = dpi_utils.scale_window_geometry(
        laptop,
        current_scale=2.5,
        candidate_scale=1.425,
        minimum_size=(1197, 855),
        preferred_size=(1482, 1054),
        work_area=(3840, 0, 7680, 2160),
        destination_position=(4200, 120),
    )
    returned = dpi_utils.scale_window_geometry(
        desktop,
        current_scale=1.425,
        candidate_scale=2.5,
        minimum_size=(2100, 1500),
        preferred_size=(2600, 1850),
        work_area=(0, 0, 3840, 2160),
        destination_position=(240, 100),
    )

    assert desktop == TkWindowGeometry(
        width=1482,
        height=1055,
        x=4200,
        y=120,
    )
    assert abs(returned.width - laptop.width) <= 1
    assert abs(returned.height - laptop.height) <= 1
    assert (returned.x, returned.y) == (240, 100)


def test_explicit_override_suppresses_monitor_recomposition():
    profile = select_presentation_profile(platform_name="win32")
    current = dpi_utils.resolve_tk_display_metrics(
        _FakeRoot(tk_scale=2.0, pixels_per_inch=144.0),
        presentation_profile=profile,
        scale_override=2.0,
    )
    candidate = dpi_utils.resolve_tk_display_metrics(
        _FakeRoot(tk_scale=3.0, pixels_per_inch=216.0),
        presentation_profile=profile,
        scale_override=3.0,
    )

    assert not dpi_utils.display_scale_changed(current, candidate)


def test_windows_native_adapter_reads_effective_window_dpi(monkeypatch):
    observed_handles = []
    fake_ctypes = SimpleNamespace(
        windll=SimpleNamespace(
            user32=SimpleNamespace(
                GetDpiForWindow=lambda handle: observed_handles.append(handle) or 240
            )
        )
    )
    monkeypatch.setattr(
        "caveviewer.gui.platform.presentation_actions.ctypes",
        fake_ctypes,
    )
    root = _FakeRoot(tk_scale=10.0 / 3.0, pixels_per_inch=240.0)

    assert WindowsPresentationActionsAdapter().window_dpi(root) == 240.0
    assert observed_handles == [321]


def test_splash_configures_awareness_before_root_and_resolves_one_metrics_value():
    source = inspect.getsource(splash_screen._show_splash_composition)

    awareness = source.index("configure_process_dpi_awareness(")
    root_creation = source.index("root = _create_splash_root(")
    metrics = source.index("display_metrics = carried_display_metrics or")

    assert awareness < root_creation < metrics
    assert "apply_tk_scaling(" not in source
    assert "tk_display_scale(" not in source
    assert "synchronize_tk_point_scale(root, display_metrics)" in source
