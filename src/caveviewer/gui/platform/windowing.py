"""Resolve viewer-window plans for the platform launch preflight."""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping

from caveviewer.core.capabilities import (
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
    requested_window_system: str | None = None,
) -> WindowBackendPlan:
    """Return the pure Linux X11/Wayland attempt order for one launch.

    This resolver intentionally reads no native APIs. The viewer-launch probe
    consumes it to report a typed capability, while the focused backend adapter
    later executes an already-authorized target.
    """
    environment = os.environ if environ is None else environ
    resolved_platform_name = sys.platform if platform_name is None else platform_name
    raw_mode = (
        str(requested_window_system).strip().lower()
        if requested_window_system is not None
        else environment.get(WINDOW_SYSTEM_ENV_VAR, "auto").strip().lower()
    )
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
        # production callers. Retain the historical Wayland attempt so the
        # authorized backend can return its actionable initialization failure.
        attempts.append(WindowSystem.WAYLAND)
    return WindowBackendPlan(mode=mode, attempts=tuple(attempts))
