"""Select CaveViewer's window backend and Linux display protocol."""

from __future__ import annotations

import enum
import importlib
import os
import sys
from dataclasses import dataclass
from types import ModuleType
from typing import Any, Callable, Mapping

from caveviewer.core.diagnostics.logging import get_logger
from caveviewer.gui.platform.app_identity import LINUX_WINDOW_INSTANCE_NAME
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
    if environment.get("DISPLAY"):
        # Prefer X11/XWayland when it is available. On GNOME this gives GLFW
        # normal compositor decorations and resize handles, and keeping this in
        # the shared policy makes source/debug launches match AppImage behavior.
        attempts.append(WindowSystem.X11)
    if environment.get("WAYLAND_DISPLAY") or environment.get("XDG_SESSION_TYPE") == "wayland":
        attempts.append(WindowSystem.WAYLAND)
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
    force_resizable_window: bool = False,
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
                    window_system=window_system,
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
                def run_glfw_window() -> None:
                    if window_size_fraction is None:
                        runner(config_class, args=["--window", "glfw"])
                    else:
                        _run_with_fixed_glfw_window_scale(
                            glfw_module,
                            lambda: runner(config_class, args=["--window", "glfw"]),
                            force_resizable_window=force_resizable_window,
                        )

                _run_with_platform_moderngl_context(
                    config_class,
                    window_system=window_system,
                    runner=run_glfw_window,
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
    window_system: WindowSystem,
    fraction: float,
    fallback: tuple[int, int],
) -> tuple[int, int]:
    """Scale the primary monitor's usable work area for a windowed launch."""
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

    content_scale = (
        _glfw_monitor_content_scale(glfw_module, monitor)
        if window_system is WindowSystem.WAYLAND
        else (1.0, 1.0)
    )
    if _wayland_workarea_needs_logical_coordinates(
        glfw_module,
        monitor,
        window_system=window_system,
        work_width=work_width,
        work_height=work_height,
        content_scale=content_scale,
    ):
        work_width = int(round(work_width / content_scale[0]))
        work_height = int(round(work_height / content_scale[1]))

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


def _glfw_monitor_content_scale(glfw_module: Any, monitor: Any) -> tuple[float, float]:
    getter = getattr(glfw_module, "get_monitor_content_scale", None)
    if not callable(getter):
        return 1.0, 1.0
    try:
        x_scale, y_scale = getter(monitor)
        x_scale = float(x_scale)
        y_scale = float(y_scale)
        if x_scale <= 0.0 or y_scale <= 0.0:
            return 1.0, 1.0
        return x_scale, y_scale
    except Exception:
        return 1.0, 1.0


def _wayland_workarea_needs_logical_coordinates(
    glfw_module: Any,
    monitor: Any,
    *,
    window_system: WindowSystem,
    work_width: int,
    work_height: int,
    content_scale: tuple[float, float],
) -> bool:
    if window_system is not WindowSystem.WAYLAND:
        return False
    x_scale, y_scale = content_scale
    if x_scale <= 1.0 and y_scale <= 1.0:
        return False

    video_mode = None
    getter = getattr(glfw_module, "get_video_mode", None)
    if callable(getter):
        try:
            video_mode = getter(monitor)
        except Exception:
            video_mode = None

    mode_width, mode_height = _video_mode_size(video_mode)
    if mode_width and mode_height:
        logical_width = mode_width / x_scale
        logical_height = mode_height / y_scale
        return work_width > logical_width * 1.2 or work_height > logical_height * 1.2

    return True


def _video_mode_size(video_mode: Any) -> tuple[int | None, int | None]:
    if video_mode is None:
        return None, None
    size = getattr(video_mode, "size", None)
    if size is not None:
        try:
            if len(size) >= 2:
                return int(size[0]), int(size[1])
        except Exception:
            pass
    width = getattr(video_mode, "width", None)
    height = getattr(video_mode, "height", None)
    if width is not None and height is not None:
        return int(width), int(height)
    if isinstance(video_mode, tuple) and len(video_mode) >= 2:
        return int(video_mode[0]), int(video_mode[1])
    return None, None


def _run_with_fixed_glfw_window_scale(
    glfw_module: Any,
    runner: Callable[[], None],
    *,
    force_resizable_window: bool = False,
) -> None:
    """Prevent ModernGL-window from scaling a relative size a second time.

    Its GLFW backend unconditionally enables SCALE_TO_MONITOR. That is useful
    for fixed pixel sizes on X11, but a size already derived from the monitor's
    work area must stay in GLFW screen coordinates. Framebuffer scaling remains
    enabled, so Wayland and other high-DPI framebuffers retain full resolution.

    When requested, also force the viewer to stay decorated and manually
    resizable. Those hints are user-visible window-management behavior, not
    rendering policy.
    """
    original_window_hint = getattr(glfw_module, "window_hint", None)
    scale_hint = getattr(glfw_module, "SCALE_TO_MONITOR", None)
    false_value = getattr(glfw_module, "FALSE", 0)
    true_value = getattr(glfw_module, "TRUE", 1)
    resizable_hint = getattr(glfw_module, "RESIZABLE", None)
    decorated_hint = getattr(glfw_module, "DECORATED", None)
    has_hint_override = scale_hint is not None or (
        force_resizable_window
        and (resizable_hint is not None or decorated_hint is not None)
    )
    if not callable(original_window_hint) or not has_hint_override:
        runner()
        return

    def window_hint(hint, value):
        if scale_hint is not None and hint == scale_hint:
            value = false_value
        elif force_resizable_window and (
            hint == resizable_hint or hint == decorated_hint
        ):
            # CaveViewer must stay manually resizable even when launched through
            # AppImage/Wayland stacks that leave sticky GLFW hints behind.
            # Decorations are part of the same user-visible contract because
            # GNOME exposes resize handles through the decorated surface.
            value = true_value
        return original_window_hint(hint, value)

    glfw_module.window_hint = window_hint
    try:
        runner()
    finally:
        glfw_module.window_hint = original_window_hint


def _run_with_platform_moderngl_context(
    config_class: type, *, window_system: WindowSystem, runner: Callable[[], None]
) -> None:
    """Give ModernGL the context detector matching the selected GLFW platform."""
    if (
        window_system is not WindowSystem.WAYLAND
        or not hasattr(config_class, "init_mgl_context")
    ):
        runner()
        return

    had_local_initializer = "init_mgl_context" in vars(config_class)
    original_initializer = vars(config_class).get("init_mgl_context")

    @classmethod
    def init_mgl_context(cls):
        import moderngl

        return moderngl.create_context(
            require=_moderngl_require_code(getattr(cls, "gl_version", (3, 3))),
            share=True,
            backend="egl",
        )

    # ModernGL-window's default Linux context detector is X11/GLX.  A GLFW
    # Wayland window exposes an EGL current context, so provide the detector
    # explicitly for this attempt and restore the config class afterward.
    config_class.init_mgl_context = init_mgl_context
    try:
        runner()
    finally:
        if had_local_initializer:
            config_class.init_mgl_context = original_initializer
        else:
            delattr(config_class, "init_mgl_context")


def _moderngl_require_code(gl_version: tuple[int, int]) -> int:
    try:
        major, minor = gl_version
        return int(major) * 100 + int(minor) * 10
    except Exception:
        return 330


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
    libdecor_hint = getattr(glfw_module, "WAYLAND_LIBDECOR", None)
    prefer_libdecor = getattr(glfw_module, "WAYLAND_PREFER_LIBDECOR", None)
    if (
        window_system is WindowSystem.WAYLAND
        and libdecor_hint is not None
        and prefer_libdecor is not None
    ):
        # GNOME's Wayland session relies on client-side decorations for normal
        # titlebar/border resize affordances. Prefer libdecor when GLFW exposes
        # the init hint so AppImage launches do not regress into borderless,
        # hard-to-resize surfaces.
        glfw_module.init_hint(libdecor_hint, prefer_libdecor)
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
        hint_string(glfw_module.X11_INSTANCE_NAME, LINUX_WINDOW_INSTANCE_NAME)

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
    if (
        "cannot detect opengl context" in message
        or "glxgetcurrentcontext" in message
        or "eglgetcurrentcontext" in message
    ):
        return True
    return isinstance(error, ValueError) and (
        "failed to initialize glfw" in message or "failed to create window" in message
    )
