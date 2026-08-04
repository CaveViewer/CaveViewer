"""Side-effect-free capability facts for opening the 3D viewer window."""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping

from caveviewer.core.capabilities import (
    CapabilityResult,
    CapabilitySource,
    ViewerLaunchRoute,
    ViewerLaunchTarget,
)

from ..windowing import (
    WINDOW_SYSTEM_ENV_VAR,
    WindowBackendError,
    resolve_window_backend_plan,
)


def probe_viewer_launch(
    *,
    environ: Mapping[str, str] | None = None,
    platform_name: str | None = None,
) -> CapabilityResult[ViewerLaunchTarget]:
    """Report the currently selectable viewer-window route without launching it.

    The probe reads only process environment and platform facts. It never
    imports or initializes GLFW, creates a test window, allocates a ModernGL
    context, or starts the render loop. Those mutable native operations remain
    at the viewer-launch action boundary after a fresh preflight authorizes a
    typed target.
    """
    environment = os.environ if environ is None else environ
    resolved_platform_name = str(platform_name or sys.platform).strip().lower()
    try:
        plan = resolve_window_backend_plan(
            environ=environment,
            platform_name=resolved_platform_name,
        )
    except WindowBackendError:
        return CapabilityResult.unavailable(
            reason_code="viewer_launch_backend_request_invalid",
            evidence={
                "requested_window_system": environment.get(
                    WINDOW_SYSTEM_ENV_VAR,
                    "auto",
                )
            },
        )
    except Exception:
        return CapabilityResult.unknown(
            reason_code="viewer_launch_capability_probe_failed",
            evidence={"probe": "window_backend_plan"},
        )

    if not resolved_platform_name.startswith("linux"):
        return CapabilityResult.available(
            ViewerLaunchTarget(
                route=ViewerLaunchRoute.NATIVE_MODERNGL,
                backend_plan=plan,
            ),
            reason_code="viewer_launch_native_route_available",
            evidence={"platform": resolved_platform_name or "unknown"},
        )

    x11_display_present = bool(environment.get("DISPLAY"))
    wayland_display_present = bool(environment.get("WAYLAND_DISPLAY")) or (
        environment.get("XDG_SESSION_TYPE", "").strip().lower() == "wayland"
    )
    if plan.mode.value == "x11" and not x11_display_present:
        return CapabilityResult.unavailable(
            reason_code="viewer_launch_x11_display_unavailable",
            evidence={"requested_window_system": plan.mode.value},
        )
    if plan.mode.value == "wayland" and not wayland_display_present:
        return CapabilityResult.unavailable(
            reason_code="viewer_launch_wayland_display_unavailable",
            evidence={"requested_window_system": plan.mode.value},
        )
    if not x11_display_present and not wayland_display_present:
        return CapabilityResult.unavailable(
            reason_code="viewer_launch_display_unavailable",
            evidence={"requested_window_system": plan.mode.value},
        )

    target = ViewerLaunchTarget(
        route=ViewerLaunchRoute.GLFW_MODERNGL,
        backend_plan=plan,
    )
    return CapabilityResult.available(
        target,
        reason_code="viewer_launch_glfw_route_available",
        source=CapabilitySource.DETECTED,
        evidence={
            "requested_window_system": plan.mode.value,
            "x11_display_present": x11_display_present,
            "wayland_display_present": wayland_display_present,
        },
    )
