"""Native backend launch composition for one immutable viewer session."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from caveviewer.core.diagnostics.logging import get_logger
from caveviewer.core.diagnostics.runtime import (
    record_runtime_exception,
    record_runtime_stage,
)
from caveviewer.gui.platform.window_backend import ViewerWindowLaunchRequest
from caveviewer.gui.viewer_session import ViewerSession


_LOG = get_logger("CaveViewer")


def run_moderngl_window_config(
    config_class: type,
    args=None,
    *,
    window_api,
    stage_recorder: Callable[..., None] = record_runtime_stage,
    exception_recorder: Callable[..., None] = record_runtime_exception,
    logger: Any = _LOG,
) -> None:
    """Run a ModernGL window and preserve cleanup on an interrupted loop."""
    config_class_name = getattr(config_class, "__name__", str(config_class))
    stage_recorder(
        "viewer_window_config_create_begin",
        config_class=config_class_name,
    )
    try:
        config = window_api.create_window_config_instance(config_class, args=args)
    except BaseException as error:
        exception_recorder(
            "viewer_window_config_create_failed",
            error,
            config_class=config_class_name,
        )
        raise
    stage_recorder(
        "viewer_window_config_created",
        config_class=config_class_name,
        window_backend=type(getattr(config, "wnd", None)).__name__,
    )
    window_destroyed_by_runner = False
    try:
        stage_recorder("viewer_window_loop_begin")
        window_api.run_window_config_instance(config)
        window_destroyed_by_runner = True
        stage_recorder("viewer_window_loop_returned")
    except BaseException as error:
        exception_recorder("viewer_window_loop_exception", error)
        wnd = getattr(config, "wnd", None)
        if wnd is not None:
            try:
                if not getattr(wnd, "is_closing", False):
                    wnd.close()
            except Exception:
                logger.exception(
                    "Error while closing viewer after interrupted window loop."
                )
        raise
    finally:
        stage_recorder(
            "viewer_window_cleanup_begin",
            loop_returned=window_destroyed_by_runner,
        )
        if not window_destroyed_by_runner:
            wnd = getattr(config, "wnd", None)
            if wnd is not None:
                try:
                    wnd.destroy()
                except Exception:
                    pass
        stage_recorder("viewer_window_cleanup_complete")


def session_window_config_class(
    session: ViewerSession,
    *,
    window_class: type,
    window_size: tuple[int, int],
    module_name: str,
) -> type:
    """Bind one immutable session to the class-based ModernGL launch API."""
    return type(
        "CaveViewerSessionWindow",
        (window_class,),
        {
            "__module__": module_name,
            "_viewer_session": session,
            "window_size": window_size,
            "vsync": session.config.vsync,
        },
    )


def launch_viewer_window(
    session: ViewerSession,
    *,
    window_size_override: tuple[int, int] | None,
    default_window_size: tuple[int, int],
    desktop_window_scale: float,
    presentation_profile,
    desktop_relative_window_size: Callable[[], tuple[int, int]],
    launch_preflight: Callable[..., Any],
    authorize_launch_target: Callable[[Any], Any],
    config_class_factory: Callable[..., type],
    runner: Callable[..., None],
    window_backend_adapter_factory: Callable[[], Any],
    stage_recorder: Callable[..., None] = record_runtime_stage,
    exception_recorder: Callable[..., None] = record_runtime_exception,
) -> None:
    """Launch with dimensions expressed in the selected backend's coordinates."""
    platform_runtime = session.config.platform_runtime
    stage_recorder("viewer_launch_preflight_begin")
    try:
        preflight = launch_preflight(platform_runtime=platform_runtime)
        target = authorize_launch_target(preflight)
    except BaseException as error:
        exception_recorder("viewer_launch_preflight_failed", error)
        raise
    stage_recorder("viewer_launch_target_authorized", route=target.route_key)

    if window_size_override is not None:
        requested_window_size = window_size_override
        window_size_fraction = None
        fallback_window_size = window_size_override
    elif presentation_profile.viewer_uses_glfw_native_initial_size:
        requested_window_size = default_window_size
        window_size_fraction = desktop_window_scale
        fallback_window_size = default_window_size
    else:
        requested_window_size = desktop_relative_window_size()
        window_size_fraction = desktop_window_scale
        fallback_window_size = default_window_size

    request = ViewerWindowLaunchRequest(
        config_class=config_class_factory(
            session,
            window_size=requested_window_size,
        ),
        runner=runner,
        window_size_fraction=window_size_fraction,
        fallback_window_size=fallback_window_size,
        force_resizable_window=True,
    )
    stage_recorder(
        "viewer_native_launch_begin",
        requested_window_size=requested_window_size,
        window_size_fraction=window_size_fraction,
    )
    try:
        window_backend_adapter_factory().launch_viewer(target, request)
    except BaseException as error:
        exception_recorder("viewer_native_launch_failed", error)
        raise
    stage_recorder("viewer_native_launch_returned")
