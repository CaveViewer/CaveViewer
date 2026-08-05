"""Immutable viewer-launch route values shared by probes and policy."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class WindowSystem(str, Enum):
    """One Linux window-system route supported by CaveViewer's GLFW launch."""

    AUTO = "auto"
    WAYLAND = "wayland"
    X11 = "x11"


@dataclass(frozen=True, slots=True)
class WindowBackendPlan:
    """Validated requested mode and ordered Linux protocol attempts.

    Non-Linux targets intentionally use an empty attempt tuple: their native
    ModernGL-window route does not select a Linux GLFW platform. Linux plans
    may contain only concrete X11 or Wayland attempts; ``AUTO`` expresses the
    requested policy, never an executable backend attempt itself.
    """

    mode: WindowSystem
    attempts: tuple[WindowSystem, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.mode, WindowSystem):
            raise TypeError("window-backend mode must be a known window system")
        if any(not isinstance(attempt, WindowSystem) for attempt in self.attempts):
            raise TypeError("window-backend attempts must be known window systems")
        if any(attempt is WindowSystem.AUTO for attempt in self.attempts):
            raise ValueError("window-backend attempts must be concrete routes")
        if self.mode is not WindowSystem.AUTO and self.attempts != (self.mode,):
            raise ValueError(
                "an explicit window-backend mode must have exactly that one attempt"
            )


class ViewerLaunchRoute(str, Enum):
    """One renderer/window-launch family available to the viewer."""

    NATIVE_MODERNGL = "native_moderngl"
    GLFW_MODERNGL = "glfw_moderngl"


@dataclass(frozen=True, slots=True)
class ViewerLaunchTarget:
    """Typed executable contract for one viewer-window launch.

    The current macOS and Windows path uses ModernGL-window's native default
    route. Linux uses a GLFW route with a concrete ordered plan. A future
    renderer such as a macOS Metal implementation can add a new route here
    without changing feature policy callers or map-opening workflows.
    """

    route: ViewerLaunchRoute
    backend_plan: WindowBackendPlan

    def __post_init__(self) -> None:
        if not isinstance(self.route, ViewerLaunchRoute):
            raise TypeError("viewer-launch route must be a known route")
        if not isinstance(self.backend_plan, WindowBackendPlan):
            raise TypeError("viewer-launch target requires a window-backend plan")
        if self.route is ViewerLaunchRoute.NATIVE_MODERNGL:
            if self.backend_plan.attempts:
                raise ValueError(
                    "the native ModernGL route must not select Linux GLFW attempts"
                )
            return
        if not self.backend_plan.attempts:
            raise ValueError("the GLFW ModernGL route requires at least one attempt")

    @property
    def route_key(self) -> str:
        """Return the stable execution-route identifier for a decision."""
        if not self.backend_plan.attempts:
            return self.route.value
        attempts = "_then_".join(
            attempt.value for attempt in self.backend_plan.attempts
        )
        return f"{self.route.value}:{attempts}"
