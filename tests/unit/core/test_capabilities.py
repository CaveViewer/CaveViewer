"""Test immutable, GUI-independent capability snapshot values."""

from __future__ import annotations

import pytest

from caveviewer.core.capabilities import (
    CapabilityResult,
    CapabilitySource,
    CapabilityStatus,
    DesktopNotificationRoute,
    DesktopNotificationTarget,
    DirectorySelectionRoute,
    DirectorySelectionTarget,
    FileSelectionRoute,
    FileSelectionTarget,
    IdleSuspendInhibitionRoute,
    IdleSuspendInhibitionTarget,
    ViewerLaunchRoute,
    ViewerLaunchTarget,
    WindowBackendPlan,
    WindowSystem,
)


def test_capability_result_copies_and_freezes_scalar_evidence():
    evidence = {"install_channel": "linux_app", "attempt": 1}

    result = CapabilityResult.available(
        "signed_manifest",
        reason_code="automatic_update_target_available",
        source=CapabilitySource.DETECTED,
        evidence=evidence,
    )
    evidence["attempt"] = 2

    assert result.status is CapabilityStatus.AVAILABLE
    assert result.value == "signed_manifest"
    assert dict(result.evidence) == {
        "install_channel": "linux_app",
        "attempt": 1,
    }
    with pytest.raises(TypeError):
        result.evidence["new"] = "value"  # type: ignore[index]


@pytest.mark.parametrize("reason_code", ("", "   "))
def test_capability_result_requires_a_stable_reason_code(reason_code):
    with pytest.raises(ValueError, match="reason_code"):
        CapabilityResult.unavailable(reason_code=reason_code)


def test_capability_result_rejects_mutable_evidence_values():
    with pytest.raises(TypeError, match="scalar diagnostics"):
        CapabilityResult.unknown(
            reason_code="probe_failed",
            evidence={"details": ["mutable"]},  # type: ignore[dict-item]
        )


def test_directory_selection_target_validates_known_distinct_routes():
    target = DirectorySelectionTarget(
        primary_route=DirectorySelectionRoute.PORTAL,
        fallback_route=DirectorySelectionRoute.TK,
    )

    assert target.route_key == "portal_then_tk"
    with pytest.raises(ValueError, match="must differ"):
        DirectorySelectionTarget(
            primary_route=DirectorySelectionRoute.TK,
            fallback_route=DirectorySelectionRoute.TK,
        )
    with pytest.raises(TypeError, match="known route"):
        DirectorySelectionTarget(primary_route="tk")  # type: ignore[arg-type]


def test_file_selection_target_validates_known_distinct_routes():
    target = FileSelectionTarget(
        primary_route=FileSelectionRoute.PORTAL,
        fallback_route=FileSelectionRoute.TK,
    )

    assert target.route_key == "portal_then_tk"
    with pytest.raises(ValueError, match="must differ"):
        FileSelectionTarget(
            primary_route=FileSelectionRoute.TK,
            fallback_route=FileSelectionRoute.TK,
        )
    with pytest.raises(TypeError, match="known route"):
        FileSelectionTarget(primary_route="tk")  # type: ignore[arg-type]


def test_desktop_notification_target_validates_known_distinct_routes():
    target = DesktopNotificationTarget(
        primary_route=DesktopNotificationRoute.PORTAL,
        fallback_route=DesktopNotificationRoute.NOOP,
    )

    assert target.route_key == "portal_then_noop"
    with pytest.raises(ValueError, match="must differ"):
        DesktopNotificationTarget(
            primary_route=DesktopNotificationRoute.NOOP,
            fallback_route=DesktopNotificationRoute.NOOP,
        )
    with pytest.raises(TypeError, match="known route"):
        DesktopNotificationTarget(primary_route="portal")  # type: ignore[arg-type]


def test_idle_suspend_inhibition_target_validates_known_distinct_routes():
    target = IdleSuspendInhibitionTarget(
        primary_route=IdleSuspendInhibitionRoute.PORTAL,
        fallback_route=IdleSuspendInhibitionRoute.NOOP,
    )

    assert target.route_key == "portal_then_noop"
    with pytest.raises(ValueError, match="must differ"):
        IdleSuspendInhibitionTarget(
            primary_route=IdleSuspendInhibitionRoute.NOOP,
            fallback_route=IdleSuspendInhibitionRoute.NOOP,
        )
    with pytest.raises(TypeError, match="known route"):
        IdleSuspendInhibitionTarget(primary_route="portal")  # type: ignore[arg-type]


def test_viewer_launch_target_validates_its_native_and_glfw_contracts():
    native_target = ViewerLaunchTarget(
        route=ViewerLaunchRoute.NATIVE_MODERNGL,
        backend_plan=WindowBackendPlan(WindowSystem.AUTO, ()),
    )
    glfw_target = ViewerLaunchTarget(
        route=ViewerLaunchRoute.GLFW_MODERNGL,
        backend_plan=WindowBackendPlan(
            WindowSystem.AUTO,
            (WindowSystem.X11, WindowSystem.WAYLAND),
        ),
    )

    assert native_target.route_key == "native_moderngl"
    assert glfw_target.route_key == "glfw_moderngl:x11_then_wayland"
    with pytest.raises(ValueError, match="must not select"):
        ViewerLaunchTarget(
            route=ViewerLaunchRoute.NATIVE_MODERNGL,
            backend_plan=WindowBackendPlan(WindowSystem.X11, (WindowSystem.X11,)),
        )
    with pytest.raises(ValueError, match="requires at least one"):
        ViewerLaunchTarget(
            route=ViewerLaunchRoute.GLFW_MODERNGL,
            backend_plan=WindowBackendPlan(WindowSystem.AUTO, ()),
        )
    with pytest.raises(ValueError, match="exactly that one"):
        WindowBackendPlan(
            WindowSystem.X11,
            (WindowSystem.X11, WindowSystem.WAYLAND),
        )
