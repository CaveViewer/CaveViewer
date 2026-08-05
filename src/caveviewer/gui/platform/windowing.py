"""Resolve viewer-window plans and retain the legacy launch compatibility façade."""

from __future__ import annotations

import os
import sys
from collections.abc import Callable, Mapping
from typing import Any

from caveviewer.core.capabilities import (
    ViewerLaunchRoute,
    ViewerLaunchTarget,
    WindowBackendPlan,
    WindowSystem,
)


WINDOW_SYSTEM_ENV_VAR = "CAVEVIEWER_WINDOW_SYSTEM"


class WindowBackendError(RuntimeError):
    """The selected GLFW/ModernGL viewer route could not be initialized."""


def resolve_window_backend_plan(
    *,
    environ: Mapping[str, str] | None = None,
    platform_name: str | None = None,
) -> WindowBackendPlan:
    """Return the pure Linux X11/Wayland attempt order for one launch.

    This resolver intentionally reads no native APIs. The viewer-launch probe
    consumes it to report a typed capability, while the focused backend adapter
    later executes an already-authorized target.
    """
    environment = os.environ if environ is None else environ
    resolved_platform_name = sys.platform if platform_name is None else platform_name
    raw_mode = environment.get(WINDOW_SYSTEM_ENV_VAR, "auto").strip().lower()
    try:
        mode = WindowSystem(raw_mode or WindowSystem.AUTO.value)
    except ValueError as exc:
        choices = ", ".join(item.value for item in WindowSystem)
        raise WindowBackendError(
            f"Invalid {WINDOW_SYSTEM_ENV_VAR}={raw_mode!r}; expected one of: {choices}."
        ) from exc

    if not resolved_platform_name.startswith("linux"):
        return WindowBackendPlan(mode=mode, attempts=())
    if mode is not WindowSystem.AUTO:
        return WindowBackendPlan(mode=mode, attempts=(mode,))

    attempts: list[WindowSystem] = []
    if environment.get("DISPLAY"):
        # Prefer X11/XWayland when it is available. On GNOME this gives GLFW
        # normal compositor decorations and resize handles, and keeping this in
        # the shared policy makes source/debug launches match AppImage behavior.
        attempts.append(WindowSystem.X11)
    if environment.get("WAYLAND_DISPLAY") or environment.get("XDG_SESSION_TYPE") == "wayland":
        attempts.append(WindowSystem.WAYLAND)
    if not attempts:
        # The capability probe will report this known missing display route to
        # production callers. Retain the historical attempt for direct legacy
        # users of this compatibility function.
        attempts.append(WindowSystem.WAYLAND)
    return WindowBackendPlan(mode=mode, attempts=tuple(attempts))


def run_window_config(
    config_class: type,
    *,
    runner: Callable[..., None],
    environ: Mapping[str, str] | None = None,
    platform_name: str | None = None,
    glfw_loader: Callable[[WindowSystem], Any] | None = None,
    window_size_fraction: float | None = None,
    fallback_window_size: tuple[int, int] | None = None,
    force_resizable_window: bool = False,
    backend_plan: WindowBackendPlan | None = None,
) -> None:
    """Compatibility façade over the focused ``WindowBackendAdapter``.

    New viewer code receives an injected adapter from ``PlatformRuntime`` and
    passes it a capability-authorized ``ViewerLaunchTarget`` directly. This
    wrapper preserves existing direct callers and focused executor tests while
    keeping all native GLFW/ModernGL work inside ``window_backend.py``.
    """
    from .window_backend import (
        ViewerWindowLaunchRequest,
        create_window_backend_adapter,
    )

    request = ViewerWindowLaunchRequest(
        config_class=config_class,
        runner=runner,
        window_size_fraction=window_size_fraction,
        fallback_window_size=fallback_window_size,
        force_resizable_window=force_resizable_window,
    )
    environment = os.environ if environ is None else environ
    resolved_platform_name = sys.platform if platform_name is None else platform_name
    plan = backend_plan or resolve_window_backend_plan(
        environ=environment,
        platform_name=resolved_platform_name,
    )
    target = ViewerLaunchTarget(
        route=(
            ViewerLaunchRoute.GLFW_MODERNGL
            if resolved_platform_name.startswith("linux")
            else ViewerLaunchRoute.NATIVE_MODERNGL
        ),
        backend_plan=plan,
    )
    create_window_backend_adapter(glfw_loader=glfw_loader).launch_viewer(
        target,
        request,
    )
