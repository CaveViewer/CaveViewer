"""Select CaveViewer's window backend and Linux display protocol."""

from __future__ import annotations

import enum
import importlib
import os
import sys
from dataclasses import dataclass
from types import ModuleType
from typing import Any, Callable, Mapping

from caveviewer.core.logging_utils import get_logger
from caveviewer.version import APPLICATION_ID


_LOG = get_logger("Windowing")
WINDOW_SYSTEM_ENV_VAR = "CAVEVIEWER_WINDOW_SYSTEM"


class WindowSystem(str, enum.Enum):
    AUTO = "auto"
    WAYLAND = "wayland"
    X11 = "x11"


class WindowBackendError(RuntimeError):
    """The requested Linux GLFW backend could not be initialized."""


@dataclass(frozen=True)
class WindowBackendPlan:
    """Validated mode and ordered protocol attempts for one viewer launch."""

    mode: WindowSystem
    attempts: tuple[WindowSystem, ...]


def resolve_window_backend_plan(
    *,
    environ: Mapping[str, str] | None = None,
    platform_name: str | None = None,
) -> WindowBackendPlan:
    environment = os.environ if environ is None else environ
    platform_name = sys.platform if platform_name is None else platform_name
    raw_mode = environment.get(WINDOW_SYSTEM_ENV_VAR, "auto").strip().lower()
    try:
        mode = WindowSystem(raw_mode or WindowSystem.AUTO.value)
    except ValueError as exc:
        choices = ", ".join(item.value for item in WindowSystem)
        raise WindowBackendError(
            f"Invalid {WINDOW_SYSTEM_ENV_VAR}={raw_mode!r}; expected one of: {choices}."
        ) from exc

    if not platform_name.startswith("linux"):
        return WindowBackendPlan(mode=mode, attempts=())
    if mode is not WindowSystem.AUTO:
        return WindowBackendPlan(mode=mode, attempts=(mode,))

    attempts: list[WindowSystem] = []
    if environment.get("WAYLAND_DISPLAY") or environment.get("XDG_SESSION_TYPE") == "wayland":
        attempts.append(WindowSystem.WAYLAND)
    if environment.get("DISPLAY"):
        attempts.append(WindowSystem.X11)
    if not attempts:
        # Let GLFW produce its actionable platform error when session
        # variables are missing instead of silently choosing another toolkit.
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
) -> None:
    """Run ModernGL-window with Linux Wayland-first GLFW selection."""
    if (
        window_size_fraction is not None
        and not 0.0 < window_size_fraction <= 1.0
    ):
        raise ValueError("window_size_fraction must be greater than 0 and at most 1")
    environment = os.environ if environ is None else environ
    platform_name = sys.platform if platform_name is None else platform_name
    plan = resolve_window_backend_plan(
        environ=environment, platform_name=platform_name
    )
    if not platform_name.startswith("linux"):
        runner(config_class, args=[])
        return

    load_glfw = glfw_loader or _load_glfw_variant
    last_error: Exception | None = None
    for attempt_index, window_system in enumerate(plan.attempts):
        glfw_module = None
        try:
            glfw_module = load_glfw(window_system)
            _prepare_glfw(glfw_module, window_system)
            if window_size_fraction is not None:
                fallback = fallback_window_size or getattr(
                    config_class, "window_size", (1280, 720)
                )
                config_class.window_size = _glfw_workarea_window_size(
                    glfw_module,
                    fraction=window_size_fraction,
                    fallback=fallback,
                )
            _LOG.info(
                "Starting GLFW viewer with %s (mode=%s).",
                window_system.value,
                plan.mode.value,
            )
            # ModernGL-window gives its environment variable precedence over
            # command-line arguments. Temporarily own it so a caller's stale
            # MODERNGL_WINDOW=pyglet cannot bypass the Linux backend policy.
            previous_window_backend = os.environ.get("MODERNGL_WINDOW")
            os.environ["MODERNGL_WINDOW"] = "glfw"
            try:
                if window_size_fraction is None:
                    runner(config_class, args=["--window", "glfw"])
                else:
                    _run_with_fixed_glfw_window_scale(
                        glfw_module,
                        lambda: runner(config_class, args=["--window", "glfw"]),
                    )
            finally:
                if previous_window_backend is None:
                    os.environ.pop("MODERNGL_WINDOW", None)
                else:
                    os.environ["MODERNGL_WINDOW"] = previous_window_backend
            return
        except Exception as exc:
            last_error = exc
            if glfw_module is not None:
                _terminate_glfw(glfw_module)
            can_retry = (
                plan.mode is WindowSystem.AUTO
                and attempt_index + 1 < len(plan.attempts)
                and _is_backend_initialization_failure(exc)
            )
            if can_retry:
                next_system = plan.attempts[attempt_index + 1]
                _LOG.warning(
                    "GLFW %s initialization failed (%s); retrying %s.",
                    window_system.value,
                    exc,
                    next_system.value,
                )
                continue
            if _is_backend_initialization_failure(exc):
                raise WindowBackendError(
                    f"Could not initialize the GLFW {window_system.value} backend: {exc}. "
                    f"Set {WINDOW_SYSTEM_ENV_VAR}=x11 or =wayland to select a backend explicitly."
                ) from exc
            raise

    raise WindowBackendError(f"Could not initialize a Linux window backend: {last_error}")


def _glfw_workarea_window_size(
    glfw_module: Any,
    *,
    fraction: float,
    fallback: tuple[int, int],
) -> tuple[int, int]:
    """Scale GLFW work-area screen coordinates for a windowed launch."""
    try:
        monitor = glfw_module.get_primary_monitor()
        if monitor is None:
            raise RuntimeError("GLFW did not report a primary monitor")
        _x, _y, work_width, work_height = glfw_module.get_monitor_workarea(monitor)
        work_width = int(work_width)
        work_height = int(work_height)
        if work_width <= 0 or work_height <= 0:
            raise RuntimeError("GLFW reported an invalid monitor work area")
    except Exception as exc:
        _LOG.warning(
            "Could not detect the GLFW monitor work area (%s); using %dx%d.",
            exc,
            *fallback,
        )
        return fallback

    window_size = (
        max(1, int(round(work_width * fraction))),
        max(1, int(round(work_height * fraction))),
    )
    _LOG.info(
        "GLFW work area is %dx%d screen coordinates; opening at %dx%d.",
        work_width,
        work_height,
        *window_size,
    )
    return window_size


def _run_with_fixed_glfw_window_scale(
    glfw_module: Any, runner: Callable[[], None]
) -> None:
    """Prevent ModernGL-window from scaling a relative size a second time.

    Its GLFW backend unconditionally enables SCALE_TO_MONITOR. That is useful
    for fixed pixel sizes on X11, but a size already derived from the monitor's
    work area must stay in GLFW screen coordinates. Framebuffer scaling remains
    enabled, so Wayland and other high-DPI framebuffers retain full resolution.
    """
    original_window_hint = getattr(glfw_module, "window_hint", None)
    scale_hint = getattr(glfw_module, "SCALE_TO_MONITOR", None)
    false_value = getattr(glfw_module, "FALSE", 0)
    if not callable(original_window_hint) or scale_hint is None:
        runner()
        return

    def window_hint(hint, value):
        if hint == scale_hint:
            value = false_value
        return original_window_hint(hint, value)

    glfw_module.window_hint = window_hint
    try:
        runner()
    finally:
        glfw_module.window_hint = original_window_hint


def _load_glfw_variant(window_system: WindowSystem) -> ModuleType:
    """Load the wheel's matching native library, reloading only for fallback."""
    os.environ["PYGLFW_LIBRARY_VARIANT"] = window_system.value
    loaded = sys.modules.get("glfw")
    if loaded is not None and getattr(loaded, "_caveviewer_variant", None) != window_system.value:
        _terminate_glfw(loaded)
        # pyGLFW selects its native library in glfw.library at import time.
        # Removing that private selector and reloading the public binding lets
        # automatic mode retry the wheel's X11 library after Wayland failure.
        sys.modules.pop("glfw.library", None)
        loaded = importlib.reload(loaded)
    if loaded is None:
        loaded = importlib.import_module("glfw")
    loaded._caveviewer_variant = window_system.value
    return loaded


def _prepare_glfw(glfw_module: Any, window_system: WindowSystem) -> None:
    target_platform = (
        glfw_module.PLATFORM_WAYLAND
        if window_system is WindowSystem.WAYLAND
        else glfw_module.PLATFORM_X11
    )
    if hasattr(glfw_module, "platform_supported") and not glfw_module.platform_supported(
        target_platform
    ):
        raise WindowBackendError(
            f"the loaded GLFW library does not support {window_system.value}"
        )
    glfw_module.init_hint(glfw_module.PLATFORM, target_platform)
    if not glfw_module.init():
        error_detail = "GLFW initialization returned false"
        try:
            _error_code, description = glfw_module.get_error()
            if description:
                error_detail = description.decode("utf-8", "replace") if isinstance(
                    description, bytes
                ) else str(description)
        except Exception:
            pass
        raise WindowBackendError(error_detail)

    # These hints must be set after glfwInit and before ModernGL-window creates
    # the native window. The backend's own second glfwInit call is harmless and
    # does not clear them.
    hint_string = getattr(glfw_module, "window_hint_string", None)
    if callable(hint_string):
        hint_string(glfw_module.WAYLAND_APP_ID, APPLICATION_ID)
        hint_string(glfw_module.X11_CLASS_NAME, APPLICATION_ID)
        hint_string(glfw_module.X11_INSTANCE_NAME, "caveviewer")

    actual_platform = getattr(glfw_module, "get_platform", lambda: target_platform)()
    if actual_platform != target_platform:
        raise WindowBackendError(
            f"GLFW selected platform {actual_platform}, expected {target_platform}"
        )


def _terminate_glfw(glfw_module: Any) -> None:
    try:
        glfw_module.terminate()
    except Exception:
        pass


def _is_backend_initialization_failure(error: Exception) -> bool:
    if isinstance(error, WindowBackendError):
        return True
    message = str(error).lower()
    return isinstance(error, ValueError) and (
        "failed to initialize glfw" in message or "failed to create window" in message
    )
