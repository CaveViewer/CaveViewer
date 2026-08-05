"""Test viewer-launch facts, policy authorization, and action-time rechecks."""

from __future__ import annotations

import pytest

from caveviewer.core.capabilities import (
    CapabilityStatus,
    ViewerLaunchRoute,
    ViewerLaunchTarget,
    WindowBackendPlan,
    WindowSystem,
)
from caveviewer.gui.platform.probes.windowing import probe_viewer_launch
from caveviewer.gui.platform.viewer_launch import (
    ViewerLaunchError,
    authorized_viewer_launch_target,
    viewer_launch_preflight,
)


def test_linux_probe_selects_existing_x11_then_wayland_plan_without_glfw_work():
    result = probe_viewer_launch(
        environ={
            "DISPLAY": ":0",
            "WAYLAND_DISPLAY": "wayland-0",
            "XDG_SESSION_TYPE": "wayland",
        },
        platform_name="linux",
    )

    assert result.status is CapabilityStatus.AVAILABLE
    assert result.value == ViewerLaunchTarget(
        route=ViewerLaunchRoute.GLFW_MODERNGL,
        backend_plan=WindowBackendPlan(
            WindowSystem.AUTO,
            (WindowSystem.X11, WindowSystem.WAYLAND),
        ),
    )
    assert result.reason_code == "viewer_launch_glfw_route_available"
    assert result.value.route_key == "glfw_moderngl:x11_then_wayland"


@pytest.mark.parametrize(
    ("environment", "reason_code"),
    [
        ({}, "viewer_launch_display_unavailable"),
        (
            {"CAVEVIEWER_WINDOW_SYSTEM": "x11"},
            "viewer_launch_x11_display_unavailable",
        ),
        (
            {"CAVEVIEWER_WINDOW_SYSTEM": "wayland"},
            "viewer_launch_wayland_display_unavailable",
        ),
        (
            {"CAVEVIEWER_WINDOW_SYSTEM": "mir"},
            "viewer_launch_backend_request_invalid",
        ),
    ],
)
def test_linux_probe_fails_closed_for_known_unusable_window_routes(
    environment,
    reason_code,
):
    result = probe_viewer_launch(environ=environment, platform_name="linux")

    assert result.status is CapabilityStatus.UNAVAILABLE
    assert result.value is None
    assert result.reason_code == reason_code


def test_non_linux_probe_keeps_the_current_native_moderngl_route():
    result = probe_viewer_launch(
        environ={"CAVEVIEWER_WINDOW_SYSTEM": "auto"},
        platform_name="darwin",
    )

    assert result.status is CapabilityStatus.AVAILABLE
    assert result.value == ViewerLaunchTarget(
        route=ViewerLaunchRoute.NATIVE_MODERNGL,
        backend_plan=WindowBackendPlan(WindowSystem.AUTO, ()),
    )
    assert result.reason_code == "viewer_launch_native_route_available"


def test_authorization_rechecks_and_rejects_a_changed_linux_route():
    environment = {
        "DISPLAY": ":0",
        "WAYLAND_DISPLAY": "wayland-0",
    }
    preflight = viewer_launch_preflight(
        environ=environment,
        platform_name="linux",
    )
    environment.pop("DISPLAY")

    with pytest.raises(ViewerLaunchError, match="availability changed"):
        authorized_viewer_launch_target(
            preflight,
            environ=environment,
            platform_name="linux",
        )


def test_disabled_preflight_never_authorizes_native_launch():
    preflight = viewer_launch_preflight(environ={}, platform_name="linux")

    with pytest.raises(ViewerLaunchError, match="no supported display"):
        authorized_viewer_launch_target(
            preflight,
            environ={},
            platform_name="linux",
        )
