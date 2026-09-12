"""Native ModernGL viewer adapter and render-thread composition root.

``CaveViewerWindow`` binds a viewer session to native backend callbacks, keeps
top-level frame ordering and shutdown explicit, and composes focused map,
capture, presentation, and input integration facets. Typed runtime objects
remain the authoritative owners of camera, streaming, capture, and GPU state;
the facets execute their context-bound work on the same native window instance
and render thread.
"""

from __future__ import annotations

from collections.abc import Mapping
import hashlib
import math
import os
import queue
import sys
import threading
import time
from typing import TYPE_CHECKING, Any

import numpy as np
import moderngl
import moderngl_window as mglw
from moderngl_window.context.base import KeyModifiers

from caveviewer.branding import BrandingAssets
from caveviewer.core.chunking import builder as chunker
from caveviewer.core.map import slicing as map_slicing
from caveviewer.core.diagnostics.logging import get_logger
from caveviewer.core.diagnostics.runtime import (
    record_runtime_exception,
    record_runtime_stage,
)
from caveviewer.core.preferences.runtime_settings import (
    RuntimeSettings,
    ViewerRuntimeSettings,
)
from caveviewer.gui.recording_capture import RecordingCaptureResources
from caveviewer.gui.render_mode_buttons import RenderModeButtons
from caveviewer.gui.controls_overlay import ControlsOverlay
from caveviewer.gui.stepper_control import StepperControl
from caveviewer.gui.color_picker import ColorPicker
from caveviewer.gui.import_progress_panel import ImportProgressPanel
from caveviewer.benchmarking.results import BenchmarkController
from caveviewer.gui.import_process import (
    start_import_process,
    terminate_import_process,
)
from caveviewer.gui.import_controller import MapImportController
from caveviewer.gui.map_opening_progress import MapOpeningProgressSession
from caveviewer.gui import recording
from caveviewer.gui import bitmap_font
from caveviewer.gui import manual_dive_trace
from caveviewer.gui.artifact_capture_controller import (
    ArtifactCapturePresentationController,
)
from caveviewer.gui.manual_dive_trace_controller import ManualDiveTraceStateController
from caveviewer.gui.slice_selection_controller import SliceSelectionController
from caveviewer.gui.slice_export_controller import (
    SliceExportController,
    SliceExportSucceeded,
)
from caveviewer.gui import recorded_dive
from caveviewer.gui import render_upload
from caveviewer.gui import viewer_input
from caveviewer.gui import viewer_bookmarks
from caveviewer.gui.recording_controller import RecordingStateController
from caveviewer.gui.viewer_action_dispatch import (
    ViewerActionDispatcher,
    ViewerKeyPressActions,
)
from caveviewer.gui.viewer_capture_workflow import (
    CaptureOwner,
    CaptureOwnershipState,
    ViewerCaptureWorkflow,
)
from caveviewer.gui.viewer_frame_scheduler import (
    ViewerFramePhase,
    ViewerFrameScheduler,
)
from caveviewer.gui.viewer_map_runtime import ViewerMapRuntime
from caveviewer.gui.viewer_capture_runtime import (
    PendingManualDiveTraceWriter as _PendingManualDiveTraceWriter,
    ViewerCaptureRuntime,
)
from caveviewer.gui.viewer_scene_presentation import ViewerScenePresentation
from caveviewer.gui.viewer_streaming_runtime import ViewerStreamingRuntime
from caveviewer.gui.viewer_session import (
    PendingImportRequest,
    ViewerBenchmarkConfig,
    ViewerLaunchMode,
    ViewerSession,
    ViewerSessionConfig,
    ViewerSessionOutcome,
)
from caveviewer.gui.viewer_workflow import (
    ViewerRenderRequest,
    ViewerWorkflowCoordinator,
    ViewerWorkflowSnapshot,
)
from caveviewer.gui.viewer_benchmark_composition import (
    environment_size as _benchmark_environment_size,
    streaming_settings_fingerprint as _benchmark_streaming_settings_fingerprint,
    streaming_settings_snapshot as _benchmark_streaming_settings_snapshot,
)
from caveviewer.gui.platform.presentation import (
    PresentationProfile,
)
from caveviewer.gui.platform.presentation_actions import PresentationActionsAdapter
from caveviewer.gui.platform.probes.recording import VideoRecordingTarget
from caveviewer.gui.platform.saved_artifact_reveal import SavedArtifactRevealAdapter
from caveviewer.gui.platform.recording_process import RecordingProcessAdapter
from caveviewer.gui.platform.desktop_inhibition import (
    acquire_idle_suspend_inhibitor,
    release_desktop_inhibitor,
)
from caveviewer.gui.platform import DesktopServiceError
from caveviewer.gui.platform.viewer_launch import (
    authorized_viewer_launch_target,
    viewer_launch_preflight,
)
from caveviewer.gui import (
    viewer_window_adapters,
    viewer_window_launch,
    viewer_window_map_integration,
)
from caveviewer.gui.viewer_window_capture_integration import (
    ViewerWindowCaptureIntegration,
)
from caveviewer.gui.viewer_window_input_integration import ViewerWindowInputIntegration
from caveviewer.gui.viewer_window_map_integration import ViewerWindowMapIntegration
from caveviewer.gui.viewer_window_presentation_integration import (
    ViewerWindowPresentationIntegration,
)
from caveviewer.gui.viewer_window_sizing import (
    DEFAULT_WINDOW_SIZE as _DEFAULT_WINDOW_SIZE,
    DESKTOP_WINDOW_SCALE as _DESKTOP_WINDOW_SCALE,
    VIEWER_UI_SCALE_MAX as _VIEWER_UI_SCALE_MAX,
    desktop_relative_window_size as _desktop_relative_window_size,
    viewer_overlay_text_scale as _viewer_overlay_text_scale,
    viewer_ui_scale_for_window_size as _viewer_ui_scale_for_window_size,
    viewer_ui_surface_size as _viewer_ui_surface_size,
    window_pixel_ratio as _window_pixel_ratio,
)
from caveviewer.gui.platform.window_backend import (
    WindowBackendAdapter,
    create_window_backend_adapter,
)
from caveviewer.resources import resource_path
from caveviewer.version import APP_NAME, APP_VERSION

if TYPE_CHECKING:
    from caveviewer.gui.platform.runtime import PlatformRuntime, VideoRecordingPreflight

_LOG = get_logger("CaveViewer")

_RENDER_UPLOAD_INITIAL_SLICE_BYTES = render_upload.RENDER_UPLOAD_INITIAL_SLICE_BYTES
# Compatibility export used by map-runtime tests and downstream diagnostics.
_VIEWER_STREAMING_SHUTDOWN_TIMEOUT_SECONDS = 2.0
_ICONIFIED_RENDER_POLL_INTERVAL_S = 0.12
_IMPORT_PAUSE_NOTICE_RENDER_INTERVAL_S = 1.0 / 30.0
_RecordingStopResult = recording.RecordingStopResult
_RecordingReadbackSlot = recording.RecordingReadbackSlot


def _map_import_inhibit_reason(map_name: str) -> str:
    """Return the desktop-visible reason used while importing a map."""
    display_name = str(map_name or "").strip() or "map"
    return f"Importing {display_name}"


def _acquire_map_import_inhibitor(
    map_name: str,
    *,
    desktop_services=None,
    platform_runtime: PlatformRuntime | None = None,
):
    """Best-effort desktop idle/suspend inhibitor for long map imports."""
    try:
        if desktop_services is None:
            from caveviewer.gui.platform import get_desktop_services

            desktop_services = get_desktop_services()
        return acquire_idle_suspend_inhibitor(
            desktop_services,
            _map_import_inhibit_reason(map_name),
            platform_runtime=platform_runtime,
        )
    except Exception as exc:
        # Legacy desktop-service construction must not block opening maps. The
        # typed acquisition boundary already handles capability and action
        # failures as no-ops once a service exists.
        _LOG.debug(
            "Desktop idle/suspend inhibitor setup skipped: error_type=%s",
            type(exc).__name__,
        )
        return None


def _release_desktop_inhibitor(inhibitor) -> None:
    """Release a desktop inhibitor without affecting import completion."""
    release_desktop_inhibitor(inhibitor)


def _env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return max(minimum, min(maximum, int(raw)))
    except ValueError:
        return default


def _env_float(name: str, default: float, minimum: float, maximum: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return max(minimum, min(maximum, float(raw)))
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    return default


def _env_optional_mebibytes(name: str) -> int | None:
    raw = os.getenv(name, "").strip()
    if not raw:
        return None
    try:
        value = float(raw)
    except ValueError:
        _LOG.warning("Ignoring invalid %s=%r; expected a positive MB value.", name, raw)
        return None
    if not math.isfinite(value) or value <= 0.0:
        _LOG.warning("Ignoring invalid %s=%r; expected a positive MB value.", name, raw)
        return None
    return max(1, int(value * 1024 ** 2))


def _map_initial_camera_position(manifest: Mapping[str, Any]) -> np.ndarray:
    """Delegate compatibility callers to the live map-start policy."""
    return viewer_window_map_integration._map_initial_camera_position(manifest)


SHADER_DIR = str(resource_path("shaders"))


def _presentation_profile_for_runtime(
    platform_runtime: PlatformRuntime | None = None,
) -> PresentationProfile:
    return viewer_window_adapters.presentation_profile_for_runtime(platform_runtime)


def _presentation_actions_adapter_for_runtime(
    platform_runtime: PlatformRuntime | None = None,
) -> PresentationActionsAdapter:
    return viewer_window_adapters.presentation_actions_adapter_for_runtime(
        platform_runtime
    )


def _window_backend_adapter_for_runtime(
    platform_runtime: PlatformRuntime | None = None,
) -> WindowBackendAdapter:
    """Use the injected viewer-window executor with a legacy direct fallback."""
    if platform_runtime is not None:
        adapter = getattr(platform_runtime, "window_backend_adapter", None)
        if adapter is not None:
            return adapter
    return create_window_backend_adapter()


def _saved_artifact_reveal_adapter_for_runtime(
    platform_runtime: PlatformRuntime | None = None,
) -> SavedArtifactRevealAdapter:
    return viewer_window_adapters.saved_artifact_reveal_adapter_for_runtime(
        platform_runtime
    )


def _recording_process_adapter_for_runtime(
    platform_runtime: PlatformRuntime | None = None,
) -> RecordingProcessAdapter:
    return viewer_window_adapters.recording_process_adapter_for_runtime(
        platform_runtime
    )


def _branding_assets_for_runtime(
    platform_runtime: PlatformRuntime | None,
) -> BrandingAssets:
    return viewer_window_adapters.branding_assets_for_runtime(platform_runtime)


def _runtime_app_icon_path(platform_runtime: PlatformRuntime | None) -> str:
    return viewer_window_adapters.runtime_app_icon_path(platform_runtime)


_UI_PANEL_VERT_SRC = """
#version 330
in vec2 in_pos;
in vec4 in_color;
out vec4 v_color;
void main() {
    gl_Position = vec4(in_pos, 0.0, 1.0);
    v_color = in_color;
}
"""

_UI_PANEL_FRAG_SRC = """
#version 330
in vec4 v_color;
out vec4 f_color;
void main() {
    f_color = v_color;
}
"""


class CaveViewerWindow(
    ViewerWindowMapIntegration,
    ViewerWindowCaptureIntegration,
    ViewerWindowPresentationIntegration,
    ViewerWindowInputIntegration,
    mglw.WindowConfig,
):
    gl_version = (3, 3)
    title = APP_NAME
    # The launch helpers replace this fallback with an 80%-of-desktop size.
    # Keep aspect_ratio unlocked so manual resizing remains fully flexible.
    window_size = _DEFAULT_WINDOW_SIZE
    resizable = True
    # The launch helpers set this from the immutable runtime snapshot. Direct
    # legacy callers retain an environment-backed fallback at launch time.
    vsync = True
    # Apply hardware anti-aliasing before compositing the cave scene and every
    # OpenGL HUD overlay into the default presentation framebuffer.
    samples = 4
    aspect_ratio = None  # don't letterbox; we recompute from actual window size

    # Global UI text scale for all bitmap_font-rendered labels. This is
    # intentionally configured here so font sizing can be adjusted from
    # one place instead of tuning every overlay module individually.
    UI_TEXT_SCALE = 1.28

    # Shared backplate behind the always-visible right-side HUD controls.
    # This keeps section labels readable over bright cave surfaces without
    # adding a separate background to every individual widget.
    RIGHT_COLUMN_PANEL_SIDE_PAD = 10
    RIGHT_COLUMN_PANEL_TOP_PAD = 8
    RIGHT_COLUMN_PANEL_BOTTOM_PAD = 10
    RIGHT_COLUMN_PANEL_RIGHT_MARGIN = 16
    RIGHT_COLUMN_PANEL_BOTTOM_MARGIN = 16
    RIGHT_COLUMN_PANEL_LABEL_GAP = 8
    RIGHT_COLUMN_PANEL_SCALE = 0.76
    RIGHT_COLUMN_PANEL_TEXT_SCALE = 0.84
    RIGHT_COLUMN_PANEL_LABEL_TEXT_SCALE = 0.98
    RIGHT_COLUMN_PANEL_BUTTON_TEXT_SCALE = 0.70
    RIGHT_COLUMN_PANEL_TEXT_MAX_UI_SCALE = 1.0
    RIGHT_COLUMN_PANEL_MAX_UI_SCALE = _VIEWER_UI_SCALE_MAX
    RIGHT_COLUMN_PANEL_FILL_RGBA = (0.09, 0.12, 0.16, 0.84)
    RIGHT_COLUMN_PANEL_BORDER_RGBA = (0.42, 0.54, 0.72, 0.62)
    RIGHT_COLUMN_PANEL_BORDER_PX = 1.5
    RECORDING_COUNTDOWN_START_NUMBER = 3
    RECORDING_COUNTDOWN_TITLE = "Prepare to record a dive"
    MANUAL_DIVE_TRACE_COUNTDOWN_START_NUMBER = 3
    MANUAL_DIVE_TRACE_COUNTDOWN_TITLE = "Prepare to plan a dive"
    SLICE_COUNTDOWN_START_NUMBER = 3
    SLICE_COUNTDOWN_TITLE = "Prepare to slice a cave"
    SLICE_PADDING = map_slicing.DEFAULT_SLICE_PADDING
    DIVE_STATUS_PANEL_WIDTH_FRACTION = 0.52
    DIVE_STATUS_PANEL_MIN_WIDTH = 520.0
    DIVE_STATUS_PANEL_HEIGHT = 116.0
    DIVE_STATUS_TITLE_PIXEL_SIZE = 2.50
    DIVE_STATUS_NOTE_PIXEL_SIZE = 1.70
    RECORDING_READBACK_BUFFER_COUNT = 3
    RECORDING_READBACK_COMPONENTS = 3
    RECORDING_RAW_PIX_FMT = "rgb24"

    # Startup focus forcing can make bundled macOS app windows appear in a
    # corner first and then jump as the window manager re-places them.
    # Default to disabled for frozen macOS builds; allow override.
    FORCE_STARTUP_FOCUS_ENV = "CAVEVIEWER_FORCE_STARTUP_FOCUS"


    # Private compatibility bridges retained for callers and tests that build
    # an uninitialized window. Production state remains owned by the typed
    # runtimes and controllers named by each property factory.

    def __init__(self, **kwargs):
        session = getattr(type(self), "_viewer_session", None)
        if not isinstance(session, ViewerSession):
            raise RuntimeError(
                "CaveViewerWindow requires a session-bound configuration class"
            )
        self._viewer_session = session
        self._window_setup_complete = False
        record_runtime_stage(
            "viewer_config_initialization_begin",
            requested_window_size=getattr(type(self), "window_size", None),
        )
        try:
            super().__init__(**kwargs)
        except BaseException as error:
            record_runtime_exception(
                "viewer_config_initialization_failed",
                error,
            )
            self._destroy_failed_window_backend()
            raise

        try:
            have_ready_cache, have_pending_import = self._initialize_window(session)
        except BaseException as error:
            record_runtime_exception(
                "viewer_config_initialization_failed",
                error,
            )
            self._cleanup_failed_initialization()
            raise

        self._window_setup_complete = True
        record_runtime_stage(
            "viewer_config_initialization_complete",
            initial_map_mode=(
                "cached" if have_ready_cache else "pending_import"
            ),
        )

    def _initialize_window(
        self, session: ViewerSession
    ) -> tuple[bool, bool]:
        """Compose one initialized window after the native context exists."""
        session_config = session.config
        self._configure_window_runtime(session_config)
        have_ready_cache, have_pending_import = self._initialize_workflow(
            session
        )
        self._initialize_window_state(session_config)
        self._initialize_pending_import_presentation(have_pending_import)
        self._initialize_shader_resources()
        self._initialize_benchmark(session_config)
        self._install_backend_modifier_probe()
        self._initialize_viewer_controls()
        self.ctx.enable(moderngl.DEPTH_TEST)
        self.ctx.enable(moderngl.CULL_FACE)
        self._initialize_startup_request(session_config, have_ready_cache)
        return have_ready_cache, have_pending_import

    def _configure_window_runtime(
        self, session_config: ViewerSessionConfig
    ) -> None:
        # moderngl-window closes its default Escape key before forwarding the
        # key callback. CaveViewer owns Escape so capture discard can finish
        # and present its result before the backend window is allowed to close.
        self._claim_backend_escape_key()
        # Pyglet's default close event destroys its native window after the
        # callback returns.  CaveViewer sometimes needs to defer that close
        # briefly (for example, while an OBJ import saves a resume point), so
        # claim the event before moderngl-window's forwarding handler runs.
        self._claim_backend_close_event()
        record_runtime_stage(
            "viewer_config_context_ready",
            context_version=getattr(getattr(self, "ctx", None), "version_code", None),
            window_backend=type(getattr(self, "wnd", None)).__name__,
        )
        self._window_setup_complete = False
        self._platform_runtime = session_config.platform_runtime
        self._branding_assets = _branding_assets_for_runtime(self._platform_runtime)
        self._runtime_settings = (
            session_config.runtime_settings
            or getattr(self._platform_runtime, "runtime_settings", None)
        )
        self._viewer_runtime_settings: ViewerRuntimeSettings | None = (
            self._runtime_settings.viewer_configuration()
            if self._runtime_settings is not None
            else None
        )
        self._presentation_profile = _presentation_profile_for_runtime(
            self._platform_runtime
        )
        self._presentation_actions_adapter = _presentation_actions_adapter_for_runtime(
            self._platform_runtime,
        )
        self._set_runtime_window_icon()

        if self._viewer_runtime_settings is None:
            force_focus_env = os.getenv(self.FORCE_STARTUP_FOCUS_ENV, "").strip().lower()
            force_focus = force_focus_env in {"1", "true", "yes", "on"}
        else:
            force_focus = self._viewer_runtime_settings.force_startup_focus
        self._startup_focus_enabled = True
        if self._presentation_profile.suppress_forced_startup_focus(
            is_frozen=bool(getattr(sys, "frozen", False)),
            force_requested=force_focus,
        ):
            self._startup_focus_enabled = False

        bitmap_font.set_presentation_profile(self._presentation_profile)
        if self._viewer_runtime_settings is None:
            bitmap_font.clear_runtime_style()
        else:
            bitmap_font.configure_runtime_style(
                font_path=self._viewer_runtime_settings.ui_font,
                antialiasing_mode=self._viewer_runtime_settings.text_antialiasing_mode,
            )
        bitmap_font.set_text_scale(
            _viewer_overlay_text_scale(
                self._presentation_profile,
                self.UI_TEXT_SCALE,
                environ={} if self._viewer_runtime_settings is not None else None,
                configured_scale=(
                    self._viewer_runtime_settings.ui_text_scale_override
                    if self._viewer_runtime_settings is not None
                    else None
                ),
            )
        )
        bitmap_font.set_raster_scale(_window_pixel_ratio(getattr(self, "wnd", None)))
        self._viewer_ui_scale = _viewer_ui_scale_for_window_size(
            _viewer_ui_surface_size(getattr(self, "wnd", None), _DEFAULT_WINDOW_SIZE),
            environ={} if self._viewer_runtime_settings is not None else None,
            configured_scale=(
                self._viewer_runtime_settings.viewer_ui_scale
                if self._viewer_runtime_settings is not None
                else None
            ),
        )
        self._right_column_panel_scale = (
            self.RIGHT_COLUMN_PANEL_SCALE * self._viewer_ui_scale
        )
        self._right_column_panel_text_scale = (
            self.RIGHT_COLUMN_PANEL_TEXT_SCALE
            * min(self._viewer_ui_scale, self.RIGHT_COLUMN_PANEL_TEXT_MAX_UI_SCALE)
        )
        self._right_column_panel_label_text_scale = (
            self.RIGHT_COLUMN_PANEL_LABEL_TEXT_SCALE
            * min(self._viewer_ui_scale, self.RIGHT_COLUMN_PANEL_TEXT_MAX_UI_SCALE)
        )
        self._right_column_panel_button_text_scale = (
            self.RIGHT_COLUMN_PANEL_BUTTON_TEXT_SCALE
            * min(self._viewer_ui_scale, self.RIGHT_COLUMN_PANEL_TEXT_MAX_UI_SCALE)
        )

    def _initialize_workflow(
        self, session: ViewerSession
    ) -> tuple[bool, bool]:
        session_config = session.config
        have_ready_cache = session_config.cache_dir is not None
        have_pending_import = session_config.pending_import is not None

        if not have_ready_cache and not have_pending_import:
            raise RuntimeError(
                "The viewer session has neither a ready cache nor a pending import."
            )

        self._workflow_coordinator = ViewerWorkflowCoordinator(session)
        return have_ready_cache, have_pending_import

    def _initialize_window_state(
        self, session_config: ViewerSessionConfig
    ) -> None:
        # Establish cleanup-safe defaults before allocating OpenGL resources.
        self._window_resources_released = False
        self.program = None
        self._hud_panel_program = None
        self._hud_panel_vbo = None
        self._hud_panel_vao = None
        self._status_panel_vbo = None
        self._status_panel_vao = None
        self._scene_presentation = None
        self.import_progress_panel = None
        self.light_stepper = None
        self.render_distance_stepper = None
        self.ambient_stepper = None
        self.render_mode_buttons = None
        self.controls_overlay = None
        self.color_picker = None
        self._pending_import_splash_rendered = False
        self._capture_runtime = ViewerCaptureRuntime(
            pending_recorded_dive_trace=session_config.recorded_dive_trace
        )
        self._initialize_interaction_and_timing_state()
        self._initialize_capture_state(session_config)
        self._initialize_map_state()
        self._initialize_import_state()

    def _initialize_interaction_and_timing_state(self) -> None:
        self._keys_down = set()
        self._last_raw_modifiers = 0
        self._mouse_look_active = False
        self._mouse_look_left_option_active = False
        self._last_mouse_pos = None
        self._frame_count = 0
        self._last_fps_print = time.time()
        self._frame_active_time_s = 0.0
        self._frame_time_history: list[float] = []
        self._last_gpu_draw_ms: float | None = None
        viewer_settings = self._viewer_runtime_settings
        self._gpu_draw_timer_enabled = (
            _env_bool("CAVEVIEWER_GPU_DRAW_TIMER", False)
            if viewer_settings is None
            else viewer_settings.gpu_draw_timer
        )
        self._streaming_frame_timing: dict | None = None
        self._last_input_reset_log = 0.0
        self._layout_cache_size: tuple | None = None
        self._layout_cache_result: dict | None = None
        self._is_iconified = False
        self._is_background_paused = False
        self._closing_requested = False
        self._startup_focus_requested = False
        self._upload_chunks_per_frame = (
            _env_int("CAVEVIEWER_UPLOAD_CHUNKS_PER_FRAME", 1, 1, 16)
            if viewer_settings is None
            else viewer_settings.streaming.upload_chunks_per_frame
        )
        self._upload_groups_per_frame = (
            _env_int("CAVEVIEWER_UPLOAD_GROUPS_PER_FRAME", 1, 1, 64)
            if viewer_settings is None
            else viewer_settings.streaming.upload_groups_per_frame
        )
        self._upload_time_budget_ms = (
            _env_float("CAVEVIEWER_UPLOAD_TIME_BUDGET_MS", 3.0, 0.5, 50.0)
            if viewer_settings is None
            else viewer_settings.streaming.upload_time_budget_ms
        )
        self._current_upload_operations_per_chunk = self._upload_groups_per_frame
        self._current_upload_time_budget_ms = self._upload_time_budget_ms
        self._vbo_upload_slice_bytes = _RENDER_UPLOAD_INITIAL_SLICE_BYTES
        self._texture_upload_slice_bytes = _RENDER_UPLOAD_INITIAL_SLICE_BYTES
        self._bookmarks_path: str | None = None
        self._bookmarks: viewer_bookmarks.BookmarkSlots = {}

    def _initialize_capture_state(
        self, _session_config: ViewerSessionConfig
    ) -> None:
        viewer_settings = self._viewer_runtime_settings
        if viewer_settings is None:
            self._recording_fps = _env_int("CAVEVIEWER_RECORDING_FPS", 30, 1, 60)
            self._recording_max_height = _env_int(
                recording.RECORDING_MAX_HEIGHT_ENV_VAR,
                recording.RECORDING_DEFAULT_MAX_HEIGHT,
                recording.RECORDING_MIN_OUTPUT_HEIGHT,
                recording.RECORDING_MAX_OUTPUT_HEIGHT,
            )
            self._recording_crf = _env_int("CAVEVIEWER_RECORDING_CRF", 23, 0, 51)
            self._recording_output_dir = os.path.expanduser(
                os.getenv(
                    "CAVEVIEWER_RECORDING_DIR",
                    os.path.join("~", "Movies", "CaveViewer"),
                )
            )
        else:
            self._recording_fps = viewer_settings.recording.fps
            self._recording_max_height = viewer_settings.recording.max_height
            self._recording_crf = viewer_settings.recording.crf
            self._recording_output_dir = os.path.expanduser(
                viewer_settings.recording.directory
            )
        self._workflow_coordinator.recording.frame_interval = (
            1.0 / float(self._recording_fps)
        )

    def _initialize_map_state(self) -> None:
        # Map-specific state (world, manifest, camera, minimap, texture manager,
        # chunk GPU objects) lives in its own method, separate
        # from the one-time-per-window setup above, so the exact same
        # logic can run again later when switching to a different map via
        # the OPEN button -- see load_new_map() / _teardown_current_map().
        self._map_runtime = ViewerMapRuntime()
        self._streaming_runtime = ViewerStreamingRuntime()
        self._pending_import_started = False
        self._initial_compilation_started_at = None
        self._initial_compilation_logged = False
        self._chunk_prep_progress = 0.0
        self._chunk_prep_complete_until = None
        self._chunk_prep_completion_armed = False
        self._main_thread_stall_last_log_at: dict[str, float] = {}
        self._window_resources_released = False

    def _initialize_import_state(self) -> None:
        # Background import state.  Import runs on a worker thread so the
        # render loop stays live (resize, repaint, vsync) the whole time.
        self._import_active: bool = False
        self._import_is_startup: bool = False
        self._import_thread: threading.Thread | None = None
        self._import_process = None
        self._import_command_queue = None
        self._import_stop_event: threading.Event | None = None
        self._import_queue: queue.Queue | None = None
        self._import_pause_requested: bool = False
        self._import_model_format: str | None = None
        self._import_map_name: str = ""
        self._import_progress_stage: str = ""
        self._import_progress_fraction: float = 0.0
        self._import_progress_title: str = ""
        self._import_progress_note: str = ""
        self._import_resuming_from_checkpoint: bool = False
        self._import_pause_notice_until: float | None = None
        self._import_pause_notice_close_after: bool = False
        self._import_pause_notice_map_name: str = ""
        self._import_pause_notice_title: str = "Import paused"
        self._import_pause_notice_stage: str = "resume point saved"
        self._import_pause_notice_note: str = ""
        self._startup_map_load_pending: tuple[
            str,
            str,
            dict,
            str | None,
        ] | None = None
        self._startup_map_load_splash_rendered = False

    def _initialize_pending_import_presentation(
        self, have_pending_import: bool
    ) -> None:
        self.import_progress_panel = None
        self._pending_import_splash_rendered = False
        if have_pending_import:
            self.import_progress_panel = ImportProgressPanel(
                self.ctx,
                branding_assets=self._branding_assets,
            )
            self._pending_import_splash_rendered = (
                self._present_pending_import_splash_now()
            )

    def _initialize_shader_resources(self) -> None:
        with open(os.path.join(SHADER_DIR, "mesh.vert")) as f:
            vert_src = f.read()
        with open(os.path.join(SHADER_DIR, "mesh.frag")) as f:
            frag_src = f.read()
        self.program = self.ctx.program(vertex_shader=vert_src, fragment_shader=frag_src)
        # u_model is always the identity matrix -- write it once here rather than
        # allocating and re-uploading a fresh identity matrix every frame.
        self.program["u_model"].write(np.identity(4, dtype=np.float32).tobytes())

        self._hud_panel_program = self.ctx.program(
            vertex_shader=_UI_PANEL_VERT_SRC,
            fragment_shader=_UI_PANEL_FRAG_SRC,
        )
        self._hud_panel_vbo = self.ctx.buffer(reserve=64 * 6 * 4)
        self._hud_panel_vao = self.ctx.vertex_array(
            self._hud_panel_program,
            [(self._hud_panel_vbo, "2f 4f", "in_pos", "in_color")],
        )
        self._status_panel_max_verts = 12000
        self._status_panel_vbo = self.ctx.buffer(reserve=self._status_panel_max_verts * 6 * 4)
        self._status_panel_vao = self.ctx.vertex_array(
            self._hud_panel_program,
            [(self._status_panel_vbo, "2f 4f", "in_pos", "in_color")],
        )

    def _initialize_benchmark(
        self, session_config: ViewerSessionConfig
    ) -> None:
        benchmark_config = session_config.benchmark
        if benchmark_config is not None:
            wnd = getattr(self, "wnd", None)
            actual_window_size = _benchmark_environment_size(
                getattr(wnd, "size", None)
            )
            actual_framebuffer_size = _benchmark_environment_size(
                getattr(wnd, "buffer_size", None)
            )
            ui_surface_size = _benchmark_environment_size(
                _viewer_ui_surface_size(
                    wnd,
                    tuple(actual_window_size)
                    if actual_window_size is not None
                    else _DEFAULT_WINDOW_SIZE,
                )
            )
            benchmark_controller = BenchmarkController(
                scenario=benchmark_config.scenario,
                output_dir=benchmark_config.output_dir,
                logger=_LOG,
                perf_counter=lambda: time.perf_counter(),
                environment=benchmark_config.environment,
            )
            benchmark_controller.update_environment(
                {
                    "gl_vendor": str(self.ctx.info.get("GL_VENDOR", "")),
                    "gl_renderer": str(self.ctx.info.get("GL_RENDERER", "")),
                    "gl_version": str(self.ctx.info.get("GL_VERSION", "")),
                    "window_backend": str(
                        getattr(getattr(self, "wnd", None), "name", "")
                    ),
                    "actual_window_size": actual_window_size,
                    "actual_framebuffer_size": actual_framebuffer_size,
                    "actual_ui_surface_size": ui_surface_size,
                    "vsync": bool(getattr(self, "vsync", False)),
                }
            )
            benchmark_controller.prepare_output()
            self._workflow_coordinator.set_benchmark_controller(
                benchmark_controller
            )

    def _initialize_viewer_controls(self) -> None:
        # Headlamp brightness control: a -/value/+ stepper, right side of
        # the screen. Replaced a draggable vertical slider -- dragging the
        # handle was unreliable for at least one person testing this
        # (clicking the track worked, grabbing the handle to drag did
        # not), so this sidesteps the whole class of problem by using
        # discrete +/-1 clicks instead of continuous drag-tracking.
        # Range/default unchanged from the old slider (0-10, default 3).
        self.light_stepper = StepperControl(
            self.ctx,
            "BRIGHTNESS",
            initial_value=5,
            min_value=0,
            max_value=10,
            text_scale=self._right_column_text_scale(),
            geometry_scale=self._right_column_geometry_scale(),
            label_text_scale=self._right_column_label_text_scale(),
        )

        # Render distance control: a -/value/+ stepper, left side of the
        # screen, mirroring the brightness control's placement logic but
        # on the opposite side. Directly drives
        # self.world.config.load_radius_cells live, same as the slider it
        # replaced. Range is 1-10 chunk-radius units. Default is 3 for a
        # balanced initial view radius without being overly aggressive on
        # memory usage. StreamingWorld's max_loaded_chunks safety valve
        # (see caveviewer.core.streaming.world) still applies underneath this as
        # a hard backstop regardless of what this is set to.
        self.render_distance_stepper = StepperControl(
            self.ctx,
            "DISTANCE",
            initial_value=3,
            min_value=1,
            max_value=10,
            text_scale=self._right_column_text_scale(),
            geometry_scale=self._right_column_geometry_scale(),
            label_text_scale=self._right_column_label_text_scale(),
        )

        # "Global illumination" control: not actual simulated light
        # bouncing (a much bigger rendering undertaking), but an even
        # ambient fill light across the WHOLE cave, independent of the
        # headlamp -- raising this washes out shadows so the cave reads
        # clearly without the headlamp doing all the work, similar to
        # what people commonly mean by a one-button "GI toggle" in
        # smaller tools. Range 0-10 maps to the shader's u_ambient float
        # (see _AMBIENT_MIN/_AMBIENT_MAX below) -- 0 reproduces the
        # original fixed ambient value this app always used (0.04, a
        # tiny fill so unlit areas aren't pure black), so leaving this at
        # its default changes nothing from before this feature existed.
        self.ambient_stepper = StepperControl(
            self.ctx,
            "GLOBAL LIGHT",
            initial_value=5,
            min_value=0,
            max_value=10,
            text_scale=self._right_column_text_scale(),
            geometry_scale=self._right_column_geometry_scale(),
            label_text_scale=self._right_column_label_text_scale(),
        )

        # Mesh/Texture toggle buttons, stacked just below the brightness
        # slider. Mesh = wireframe overlay on/off; Texture = whether the
        # photo texture is sampled or the surface falls back to plain lit
        # gray. See caveviewer.gui.render_mode_buttons for the four resulting
        # combined display states.
        self.render_mode_buttons = RenderModeButtons(
            self.ctx,
            texture_enabled=True,
            wireframe_enabled=False,
            smooth_shading_enabled=True,
            text_scale=self._right_column_button_text_scale(),
            geometry_scale=self._right_column_geometry_scale(),
        )
        # Loading-policy lock for right-side button effects. While a map
        # is loading, all render-mode toggles are forced off; once
        # loading completes, defaults become Texture ON, Mesh OFF,
        # Shade OFF until explicitly enabled by the user.
        self._render_mode_load_lock_active = False

        # Controls reference / loading overlay -- full-screen right now
        # while the first chunks around the spawn point stream in, and
        # again as a smaller panel any time a minimap click teleports the
        # camera somewhere new (see on_mouse_press_event's minimap-click
        # handling, which calls self.controls_overlay.show_panel()).
        self.controls_overlay = ControlsOverlay(
            self.ctx,
            presentation_profile=self._presentation_profile,
            branding_assets=self._branding_assets,
        )
        self.controls_overlay.show_fullscreen()

        # Background ("void") color picker, toggled via the COLOR button.
        # Defaults to the same near-black the viewer always used, so
        # nothing changes for anyone who never opens it.
        self.color_picker = ColorPicker(self.ctx, initial_color=(0.02, 0.02, 0.03))

        # Shown only while a newly-opened map is being imported/chunked
        # for the first time (see _handle_open_button_click) -- never
        # active during normal viewing, so it has no on/off state of its
        # own the way the other overlays do.
        if self.import_progress_panel is None:
            self.import_progress_panel = ImportProgressPanel(
                self.ctx,
                branding_assets=self._branding_assets,
            )

    def _initialize_startup_request(
        self, session_config: ViewerSessionConfig, have_ready_cache: bool
    ) -> None:
        if have_ready_cache:
            self._startup_map_load_pending = (
                session_config.cache_dir,
                session_config.textures_dir,
                session_config.manifest,
                session_config.map_root,
            )
        # else: have_pending_import is true instead -- the actual import
        # is deliberately NOT run here, before the window has rendered
        # even one frame. It's triggered from inside on_render() instead
        # (see _run_pending_import), once the window is confirmed to
        # actually be open and able to draw the in-window progress panel
        # -- starting the blocking import here, before super().__init__()
        # has truly finished and the window is on screen, would risk the
        # exact same "nothing to draw into yet" problem this feature
        # exists to avoid.

    def _cleanup_failed_initialization(self) -> None:
        """Release resources created before a constructor failure."""
        self._window_setup_complete = False
        if hasattr(self, "_window_resources_released"):
            try:
                self._release_window_resources()
            except Exception:
                _LOG.exception(
                    "Error while releasing a partially initialized viewer."
                )
        workflows = getattr(self, "_workflow_coordinator", None)
        if workflows is not None:
            try:
                workflows.complete_shutdown()
            except Exception:
                _LOG.exception("Error while closing partial viewer workflows.")
        self._destroy_failed_window_backend()

    def _destroy_failed_window_backend(self) -> None:
        """Best-effort native cleanup when WindowConfig construction fails."""
        wnd = getattr(self, "wnd", None)
        if wnd is None or not hasattr(wnd, "destroy"):
            return
        try:
            wnd.destroy()
        except Exception:
            _LOG.exception("Error while destroying a failed viewer window.")

    def _active_presentation_profile(self) -> PresentationProfile:
        """Return the immutable UI profile for this viewer instance."""
        profile = getattr(self, "_presentation_profile", None)
        if profile is None:
            profile = _presentation_profile_for_runtime(
                getattr(self, "_platform_runtime", None)
            )
            self._presentation_profile = profile
        return profile

    def _active_presentation_actions_adapter(self) -> PresentationActionsAdapter:
        """Return native presentation actions without reusing static policy."""
        actions = getattr(self, "_presentation_actions_adapter", None)
        if actions is None:
            actions = _presentation_actions_adapter_for_runtime(
                getattr(self, "_platform_runtime", None),
            )
            self._presentation_actions_adapter = actions
        return actions

    def _active_saved_artifact_reveal_adapter(self) -> SavedArtifactRevealAdapter:
        """Return the runtime action adapter or compose a direct fallback."""
        return _saved_artifact_reveal_adapter_for_runtime(
            getattr(self, "_platform_runtime", None)
        )

    def _active_recording_process_adapter(self) -> RecordingProcessAdapter:
        """Return the runtime launch adapter or compose a direct fallback."""
        return _recording_process_adapter_for_runtime(
            getattr(self, "_platform_runtime", None)
        )

    def _active_benchmark_controller(self) -> BenchmarkController | None:
        """Return an injected test controller or the session-owned controller."""
        controller = self.__dict__.get("_benchmark_controller")
        if controller is not None:
            return controller
        workflows = self.__dict__.get("_workflow_coordinator")
        return None if workflows is None else workflows.benchmark_controller

    def _finish_benchmark(self, *, reason: str) -> bool:
        """Finish benchmark output through its session lifecycle owner."""
        workflows = self.__dict__.get("_workflow_coordinator")
        if workflows is not None:
            return workflows.finish_benchmark(reason=reason)
        controller = self.__dict__.get("_benchmark_controller")
        if controller is None or controller.finished:
            return False
        controller.finish(reason=reason)
        return True

    def _acquire_import_inhibitor(self, map_name: str):
        """Use the runtime's shared desktop service for a map-import action."""
        runtime = getattr(self, "_platform_runtime", None)
        if runtime is None:
            return _acquire_map_import_inhibitor(map_name)
        return _acquire_map_import_inhibitor(
            map_name,
            desktop_services=runtime.desktop_services,
            platform_runtime=runtime,
        )

    def _ensure_import_controller(self) -> MapImportController:
        controller = self.__dict__.get("_import_controller")
        if controller is not None:
            return controller

        runtime_settings = getattr(self, "_runtime_settings", None)

        def launch_import_process(model_descriptor: dict, textures_dir: str):
            if runtime_settings is None:
                return start_import_process(model_descriptor, textures_dir)
            return start_import_process(
                model_descriptor,
                textures_dir,
                runtime_settings=runtime_settings.import_configuration(),
            )

        def create_controller() -> MapImportController:
            return MapImportController(
                self,
                logger=lambda: _LOG,
                chunker=lambda: chunker,
                start_import_process=lambda: launch_import_process,
                terminate_import_process=lambda: terminate_import_process,
                acquire_inhibitor=lambda: self._acquire_import_inhibitor,
                release_inhibitor=lambda: _release_desktop_inhibitor,
                perf_counter=lambda: time.perf_counter(),
                monotonic=lambda: time.monotonic(),
                report_startup_failure=self._record_startup_import_failure,
            )

        workflows = self.__dict__.get("_workflow_coordinator")
        if workflows is not None:
            return workflows.ensure_import_controller(create_controller)
        controller = create_controller()
        self.__dict__["_import_controller"] = controller
        return controller

    def _record_startup_import_failure(self, message: str, suggestion: str) -> None:
        """Preserve a recoverable failure across native-window teardown."""
        self._viewer_session.record_outcome(
            kind="import_failed",
            message=message,
            suggestion=suggestion,
        )

    def _ensure_recording_controller(self) -> RecordingStateController:
        controller = self.__dict__.get("_recording_controller")
        if controller is None:
            workflows = self.__dict__.get("_workflow_coordinator")
            if workflows is not None:
                return workflows.recording
            controller = self.__dict__.setdefault(
                "_recording_controller",
                RecordingStateController(),
            )
        return controller

    def _ensure_frame_scheduler(self) -> ViewerFrameScheduler:
        """Return the non-GL frame phase and throttling coordinator."""
        scheduler = self.__dict__.get("_frame_scheduler")
        if scheduler is None:
            workflows = self.__dict__.get("_workflow_coordinator")
            if workflows is not None:
                return workflows.frame_scheduler
            scheduler = self.__dict__.setdefault(
                "_frame_scheduler",
                ViewerFrameScheduler(),
            )
        return scheduler

    def _ensure_capture_workflow(self) -> ViewerCaptureWorkflow:
        """Return the non-GL workflow shared by the capture controllers."""
        workflow = self.__dict__.get("_capture_workflow")
        if workflow is None:
            workflows = self.__dict__.get("_workflow_coordinator")
            if workflows is not None:
                return workflows.capture
            workflow = self.__dict__.setdefault(
                "_capture_workflow",
                ViewerCaptureWorkflow(),
            )
        return workflow

    def _ensure_capture_runtime(self) -> ViewerCaptureRuntime:
        """Return the session's render-thread capture execution owner."""
        runtime = getattr(self, "_capture_runtime", None)
        if runtime is None:
            runtime = ViewerCaptureRuntime()
            self._capture_runtime = runtime
        return runtime

    def _ensure_scene_presentation(self) -> ViewerScenePresentation:
        """Return the render-thread scene and shared HUD drawing owner."""
        presentation = getattr(self, "_scene_presentation", None)
        if presentation is None:
            presentation = ViewerScenePresentation(
                ctx=self.ctx,
                program=self.program,
                hud_panel_vbo=self._hud_panel_vbo,
                hud_panel_vao=self._hud_panel_vao,
                status_panel_vbo=self._status_panel_vbo,
                status_panel_vao=self._status_panel_vao,
                perf_counter=time.perf_counter,
            )
            self._scene_presentation = presentation
        else:
            presentation.ctx = self.ctx
            presentation.program = self.program
            presentation.hud_panel_vbo = self._hud_panel_vbo
            presentation.hud_panel_vao = self._hud_panel_vao
            presentation.status_panel_vbo = self._status_panel_vbo
            presentation.status_panel_vao = self._status_panel_vao
        return presentation

    def _ensure_action_dispatcher(self) -> ViewerActionDispatcher:
        """Return the ordered key-action coordinator for this viewer session."""
        dispatcher = self.__dict__.get("_action_dispatcher")
        if dispatcher is None:
            workflows = self.__dict__.get("_workflow_coordinator")
            if workflows is not None:
                return workflows.actions
            dispatcher = self.__dict__.setdefault(
                "_action_dispatcher",
                ViewerActionDispatcher(),
            )
        return dispatcher

    def _ensure_manual_dive_trace_controller(self) -> ManualDiveTraceStateController:
        controller = self.__dict__.get("_manual_dive_trace_controller")
        if controller is None:
            workflows = self.__dict__.get("_workflow_coordinator")
            if workflows is not None:
                return workflows.manual_dive_trace
            controller = self.__dict__.setdefault(
                "_manual_dive_trace_controller",
                ManualDiveTraceStateController(),
            )
        return controller

    def _ensure_slice_selection_controller(self) -> SliceSelectionController:
        controller = self.__dict__.get("_slice_selection_controller")
        if controller is None:
            workflows = self.__dict__.get("_workflow_coordinator")
            if workflows is not None:
                return workflows.slice_selection
            controller = self.__dict__.setdefault(
                "_slice_selection_controller",
                SliceSelectionController(),
            )
        return controller

    def _ensure_slice_export_controller(self) -> SliceExportController:
        controller = self.__dict__.get("_slice_export_controller")
        if controller is None:
            workflows = self.__dict__.get("_workflow_coordinator")
            if workflows is not None:
                return workflows.slice_export
            controller = self.__dict__.setdefault(
                "_slice_export_controller",
                SliceExportController(),
            )
        return controller

    def _workflow_snapshot(self) -> ViewerWorkflowSnapshot:
        """Adapt render-thread state for the non-GL workflow coordinator."""
        manual_trace = self._ensure_manual_dive_trace_controller()
        slice_selection = self._ensure_slice_selection_controller()
        slice_export = self._ensure_slice_export_controller()
        recording_armed = self._recording_is_armed()
        return ViewerWorkflowSnapshot(
            setup_complete=bool(getattr(self, "_window_setup_complete", False)),
            closing_requested=bool(getattr(self, "_closing_requested", False)),
            iconified=bool(getattr(self, "_is_iconified", False)),
            import_active=bool(getattr(self, "_import_active", False)),
            map_loaded=bool(getattr(self, "_has_map_loaded", False)),
            capture_close_pending=self._capture_close_pending(),
            recording_owned=(
                recording_armed or self._recording_stop_in_progress()
            ),
            recording_armed=recording_armed,
            recording_active=(
                getattr(self, "_recording_session", None) is not None
            ),
            manual_dive_trace_countdown_active=manual_trace.countdown_active,
            manual_dive_trace_active=(
                getattr(self, "_manual_dive_trace", None) is not None
            ),
            manual_dive_trace_finalizing=bool(
                getattr(self, "_manual_dive_trace_writers", None)
            ),
            slice_countdown_active=slice_selection.countdown_active,
            slice_selection_active=slice_selection.selection_active,
            slice_saving=slice_selection.saving,
            slice_export_active=slice_export.active,
        )

    def _workflow_render_request(self) -> ViewerRenderRequest | None:
        """Return aggregate non-GL decisions for a production viewer session."""
        workflows = self.__dict__.get("_workflow_coordinator")
        if workflows is None:
            return None
        return workflows.render_request(self._workflow_snapshot())

    def _slice_work_pending(self) -> bool:
        """Return whether a countdown or child export needs a frame-time poll."""
        request = self._workflow_render_request()
        if request is not None:
            return request.slice_work_pending
        selection = self.__dict__.get("_slice_selection_controller")
        exporter = self.__dict__.get("_slice_export_controller")
        return bool(
            selection is not None and selection.countdown_active
        ) or bool(exporter is not None and exporter.active)

    def _slice_interaction_active(self) -> bool:
        """Return whether slice selection owns the capture interaction surface."""
        request = self._workflow_render_request()
        if request is not None:
            return request.slice_interaction_active
        selection = self.__dict__.get("_slice_selection_controller")
        exporter = self.__dict__.get("_slice_export_controller")
        return bool(
            selection is not None
            and (
                selection.countdown_active
                or selection.selection_active
                or selection.saving
            )
        ) or bool(exporter is not None and exporter.active)

    def _capture_ownership_state(self) -> CaptureOwnershipState:
        """Return all lifecycle owners used to enforce one capture at a time."""
        return CaptureOwnershipState(
            recording_owned=(
                self._recording_is_armed() or self._recording_stop_in_progress()
            ),
            manual_dive_trace_owned=(
                self._ensure_manual_dive_trace_controller().countdown_active
                or getattr(self, "_manual_dive_trace", None) is not None
                or bool(getattr(self, "_manual_dive_trace_writers", None))
            ),
            slice_owned=self._slice_interaction_active(),
        )

    def _capture_owner(self) -> CaptureOwner | None:
        """Return the countdown, active capture, or finalizer owning capture."""
        request = self._workflow_render_request()
        if request is not None:
            return request.capture_owner
        return self._ensure_capture_workflow().owner_for(
            self._capture_ownership_state()
        )

    def _capture_start_blocked(self, requested_owner: CaptureOwner) -> bool:
        """Reject a second capture while the current owner is still cleaning up."""
        owner = self._capture_owner()
        if owner is None:
            return False
        owner_name = {
            CaptureOwner.VIDEO: "video recording",
            CaptureOwner.DIVE_TRACE: "dive trace",
            CaptureOwner.SLICE: "cave slice",
        }[owner]
        requested_name = {
            CaptureOwner.VIDEO: "video recording",
            CaptureOwner.DIVE_TRACE: "dive trace",
            CaptureOwner.SLICE: "cave slice",
        }[requested_owner]
        self._show_capture_status(
            "Capture in progress",
            (
                f"Finish or cancel the current {owner_name} before starting "
                f"a new {requested_name}."
            ),
            kind="info",
            duration=3.0,
        )
        return True

    def _capture_shortcut_is_ignored(self, requested_owner: CaptureOwner) -> bool:
        """Consume a foreign capture shortcut without presentation side effects."""
        return self._ensure_capture_workflow().should_ignore_capture_shortcut(
            active_owner=self._capture_owner(),
            requested_owner=requested_owner,
        )

    def _active_capture_owner(self) -> CaptureOwner | None:
        """Return the owner that is actively collecting a video, trace, or slice."""
        request = self._workflow_render_request()
        if request is not None:
            return request.active_capture_owner
        selection = self.__dict__.get("_slice_selection_controller")
        return self._ensure_capture_workflow().owner_for(
            CaptureOwnershipState(
                recording_owned=(
                    getattr(self, "_recording_session", None) is not None
                ),
                manual_dive_trace_owned=(
                    getattr(self, "_manual_dive_trace", None) is not None
                ),
                slice_owned=bool(
                    selection is not None and selection.selection_active
                ),
            )
        )

    def _render_active_capture_instruction(
        self,
        window_size: tuple[int, int],
    ) -> bool:
        """Render guidance only if capture policy supplies a persistent banner."""
        instruction = self._ensure_capture_workflow().instruction_for(
            self._active_capture_owner(),
            primary_shortcut_label=self._primary_shortcut_label(),
        )
        if instruction is None:
            return False
        self._render_dive_status_prompt(
            window_size,
            title=instruction.title,
            note=instruction.note,
        )
        return True

    def _ensure_artifact_capture_presentation(
        self,
    ) -> ArtifactCapturePresentationController:
        """Return the shared post-save feedback and reveal scheduler."""
        controller = self.__dict__.get("_artifact_capture_presentation")
        if controller is None:
            workflows = self.__dict__.get("_workflow_coordinator")
            if workflows is not None:
                return workflows.artifact_presentation
            controller = self.__dict__.setdefault(
                "_artifact_capture_presentation",
                ArtifactCapturePresentationController(),
            )
        return controller

    def _ensure_recording_capture(self) -> RecordingCaptureResources:
        runtime = self._ensure_capture_runtime()
        return runtime.ensure_recording_resources(
            ctx=getattr(self, "ctx", None),
            buffer_count=self.RECORDING_READBACK_BUFFER_COUNT,
            readback_components=self.RECORDING_READBACK_COMPONENTS,
            logger=_LOG,
            perf_counter=time.perf_counter,
        )

    def _claim_backend_escape_key(self) -> None:
        """Disable the backend's preemptive Escape close callback."""
        self.wnd.exit_key = None

    def _claim_backend_close_event(self) -> None:
        """Route Pyglet close requests through CaveViewer's deferred workflow.

        Returning ``True`` is Pyglet's ``EVENT_HANDLED`` sentinel.  Without it,
        Pyglet invokes its default close handler after our callback and sets
        ``has_exit`` even when :meth:`on_close` has deferred shutdown.
        """
        backend = getattr(self, "wnd", None)
        native_window = getattr(backend, "_window", None)
        push_handlers = getattr(native_window, "push_handlers", None)
        if getattr(backend, "name", None) != "pyglet" or not callable(push_handlers):
            return

        def on_close() -> bool:
            self.on_close()
            return True

        push_handlers(on_close=on_close)

    def _set_runtime_window_icon(self) -> None:
        """Set the native viewer-window icon when the backend exposes one."""
        viewer_settings = getattr(self, "_viewer_runtime_settings", None)
        icon_path = (
            viewer_settings.app_icon
            if viewer_settings is not None and viewer_settings.app_icon
            else _runtime_app_icon_path(getattr(self, "_platform_runtime", None))
        )
        if not os.path.exists(icon_path):
            _LOG.warning(f"viewer window icon asset not found: {icon_path}")
            return

        targets = []
        for target in (getattr(self, "wnd", None), getattr(getattr(self, "wnd", None), "_window", None)):
            if target is not None and target not in targets:
                targets.append(target)

        for target in targets:
            set_icon = getattr(target, "set_icon", None)
            if not callable(set_icon):
                continue
            try:
                # Try passing the path directly first â€” some pyglet versions
                # (and some backends) expect a filename/Path rather than a
                # pre-loaded ImageData object and will call .is_absolute() on
                # the argument, which fails on ImageData.
                set_icon(icon_path)
                _LOG.info("Set viewer window icon.")
                return
            except Exception:
                pass
            try:
                import pyglet
                icon = pyglet.image.load(icon_path)
                set_icon(icon)
                _LOG.info("Set viewer window icon.")
                return
            except Exception as e:
                _LOG.warning(f"could not set viewer window icon ({e}); continuing without it.")
                return

        _LOG.debug("viewer backend does not expose a set_icon() hook.")


    # -- chunk GPU lifecycle ------------------------------------------------


    # -- moderngl_window hooks ------------------------------------------------
    #
    # moderngl-window renamed its per-frame/event hooks across major versions
    # (older releases used bare names like render()/key_event(), 3.x renamed
    # them to on_render()/on_key_event() etc). To work across versions without
    # guessing which exact release someone has installed, each hook below is
    # implemented under the new on_* name and aliased to the old bare name.

    # Right-side column layout: brightness stepper, then render-distance
    # stepper, then the Mesh/Texture/Help/Color/Open button block, all
    # stacked vertically and anchored as ONE group to the bottom-right
    # corner of the window (moved here from separate top-anchored
    # positions per request). Computed in this single method, used
    # identically by render() and the mouse-press handler, so the
    # clickable areas can never drift out of sync with what's actually
    # drawn -- the same reasoning the old per-control anchor helpers
    # already followed, just now covering the whole column at once since
    # a bottom anchor means every piece's position depends on the total
    # height of everything below the WINDOW bottom margin, not just its
    # own height.
    RIGHT_COLUMN_BOTTOM_MARGIN = 18
    RIGHT_COLUMN_GAP = 10  # vertical gap between the right-side HUD blocks
    RIGHT_COLUMN_BUTTON_GROUP_GAP = 20  # extra gap before the Mesh/Texture/Shade group

    # Keyboard look fallback (especially useful on macOS hardware where
    # right-button drag can be awkward/unavailable). Interpreted as
    # virtual mouse pixels per second and passed through camera.look().
    _KEY_LOOK_PIXELS_PER_SECOND = 700.0

    # Maps the GLOBAL LIGHT stepper's 0-10 integer range onto the
    # shader's actual u_ambient float. 0 -> _AMBIENT_MIN reproduces the
    # exact fixed ambient value this app always used before this feature
    # existed (a tiny fill so unlit areas aren't pure black, not truly
    # zero) -- so the default stepper value of 0 changes nothing for
    # anyone who never touches this control. 10 -> _AMBIENT_MAX is a
    # strong, even fill bright enough to read the whole cave clearly
    # without the headlamp doing any of the work, without fully blowing
    # out texture detail into flat white.
    _AMBIENT_MIN = 0.04
    _AMBIENT_MAX = 0.9
    _INITIAL_LOAD_MIN_CHUNKS = 6
    _INITIAL_VISUAL_READY_SETTLE_FRAMES = 3
    _STARTUP_VISUAL_RADIUS_EXTRA_CHUNKS = 3
    _STARTUP_VISUAL_RADIUS_MAX_CHUNKS = 10
    _CHUNK_PREP_MAX_FRACTION = 0.97
    _CHUNK_PREP_COMPLETE_HOLD_SECONDS = 0.85
    _STREAMING_FAILURES_PER_FRAME = 8


    def on_render(self, current_time: float, frame_time: float):
        if self._frame_phase() is ViewerFramePhase.INACTIVE:
            return
        if not getattr(self, "_first_render_checkpoint_recorded", False):
            self._first_render_checkpoint_recorded = True
            record_runtime_stage(
                "viewer_first_render_entered",
                window_size=getattr(getattr(self, "wnd", None), "size", None),
            )

        # Backends can miss iconify callbacks on Dock minimize; poll a
        # few common window flags each frame as a safety net.
        runtime_iconified = self._query_runtime_iconified_state()
        self._set_background_pause(runtime_iconified, "runtime window state")

        frame_phase = self._frame_phase()
        frame_scheduler = self._ensure_frame_scheduler()
        if frame_phase is ViewerFramePhase.ICONIFIED:
            # Keep minimize mode cheap: no streaming updates/uploads while
            # iconified.  Poll low-frequency completion state without blocking
            # the render/window callback.
            if frame_scheduler.is_due(
                "iconified",
                _ICONIFIED_RENDER_POLL_INTERVAL_S,
                now=time.perf_counter(),
            ):
                self._drain_recording_stop_results()
                if self._slice_work_pending():
                    self._update_slice_export()
                if self._capture_close_pending():
                    self._update_manual_dive_trace()
                    if self._complete_escape_capture_cancellation_if_ready():
                        return
                    self._complete_exit_capture_finalization_if_ready(
                        allow_unpresented_status=True
                    )
                self._drain_due_saved_artifact_reveals()
            return
        frame_scheduler.reset_throttle("iconified")

        bitmap_font.set_raster_scale(_window_pixel_ratio(self.wnd))

        # Keep render-mode button effects synced to loading state even
        # on frames that early-return before normal HUD interaction.
        self._sync_render_mode_loading_policy()
        self._drain_recording_stop_results()
        self._drain_due_saved_artifact_reveals()

        if frame_phase is ViewerFramePhase.FINALIZING_CAPTURE:
            self._update_manual_dive_trace()
            if self._slice_work_pending():
                self._update_slice_export()
            if self._complete_escape_capture_cancellation_if_ready():
                return
            if self._complete_exit_capture_finalization_if_ready():
                return
            # Do not continue navigation, streaming, or map interaction while
            # a user-visible artifact is still being published. The centered
            # status keeps the same visual hierarchy as import and capture UI.
            self.ctx.clear(0.02, 0.02, 0.03)
            self._render_capture_status_message(self.wnd.size)
            return

        if self._startup_focus_enabled:
            self._request_startup_focus_once()

        # Background import in flight: drain worker results on every callback
        # and redraw the progress panel every callback. Window backends may
        # still present/swap after this method returns, so skipping draws here
        # can expose stale back buffers as visible flicker during first-time
        # imports.
        if frame_phase is ViewerFramePhase.IMPORTING:
            self._drain_import_queue()
            if not self._import_active:
                return
            self.ctx.clear(0.02, 0.02, 0.03)
            fraction = self._import_progress_fraction
            # When the real fraction is near zero (numpy is crunching
            # faces and can't report sub-step progress), pulse the indicator
            # gently between 0 and 2 % so it looks alive.  The pulse is
            # capped below the first real progress step (3 %) so the
            # max() inside import_progress_panel takes over cleanly once
            # measurable progress begins.
            if fraction < 0.021:
                t = time.perf_counter()
                fraction = abs(math.sin(t * 1.2)) * 0.02
            import_controller = self._ensure_import_controller()
            frame = self._ensure_map_opening_progress_session().observe_import(
                self._import_map_name,
                self._import_progress_stage,
                fraction,
                note=self._import_progress_note,
                supporting_note_override=import_controller.transient_progress_note(),
            )
            self._render_map_opening_progress(frame)
            return
        frame_scheduler.reset_throttle("import_progress")

        if frame_phase is ViewerFramePhase.STARTUP:
            if getattr(self, "_startup_map_load_pending", None) is not None:
                self._load_startup_map_after_splash()
                return
            if frame_scheduler.is_due(
                "import_pause_notice",
                _IMPORT_PAUSE_NOTICE_RENDER_INTERVAL_S,
                now=time.perf_counter(),
            ):
                if self._render_import_pause_notice_if_active():
                    return
                frame_scheduler.reset_throttle("import_pause_notice")
            elif getattr(self, "_import_pause_notice_until", None) is not None:
                return
            # First frame with no map loaded yet: draw the loading panel
            # immediately so the user sees the logo instead of a blank window.
            # The actual import starts on the next frame so the splash has a
            # chance to present before import startup work contends with the
            # render loop.
            if self._pending_import_started:
                return
            self._render_pending_import_splash()
            if not self._pending_import_splash_rendered:
                self._pending_import_splash_rendered = True
                return
            self._pending_import_started = True
            self._run_pending_import()
            return

        self._render_interactive_frame(current_time, frame_time)


    render = on_render  # back-compat alias for older moderngl-window releases


    def on_key_event(self, key, action, modifiers: KeyModifiers):
        # Cocoa may dispatch key callbacks before viewer controls exist or
        # after teardown has started. Input is not actionable in either state.
        if (
            not getattr(self, "_window_setup_complete", False)
            or self._input_is_suppressed()
        ):
            return

        if self.controls_overlay is None:
            return
        keys = self.wnd.keys
        if action == keys.ACTION_PRESS:
            actions = ViewerKeyPressActions(
                window_shortcut=lambda: self._handle_window_shortcut(key, modifiers),
                recorded_dive=lambda: self._handle_recorded_dive_hotkey(
                    key, modifiers
                ),
                begin_screen=lambda: self._handle_begin_screen_hotkey(key),
                capture_escape=lambda: self._handle_capture_escape_hotkey(key),
                fly_speed=lambda: self._handle_fly_speed_hotkey(key, modifiers),
                bookmark=lambda: self._handle_bookmark_hotkey(key, modifiers),
                manual_dive_trace=lambda: self._handle_manual_dive_trace_hotkey(
                    key, modifiers
                ),
                slice=lambda: self._handle_slice_hotkey(key, modifiers),
                recording=lambda: self._handle_recording_hotkey(key, modifiers),
                reset_view=lambda: self._handle_reset_view_shortcut(key, modifiers),
            )
            workflows = self.__dict__.get("_workflow_coordinator")
            action_handled = (
                workflows.dispatch_key_press(actions)
                if workflows is not None
                else self._ensure_action_dispatcher().dispatch_key_press(actions)
            )
            if action_handled:
                return
            self._keys_down.add(key)
        elif viewer_input.key_event_is_press_or_repeat(keys, action):
            repeat_args = {
                "waiting_for_begin": self.controls_overlay.is_waiting_for_begin,
                "fly_speed": lambda: self._handle_fly_speed_hotkey(key, modifiers),
            }
            workflows = self.__dict__.get("_workflow_coordinator")
            if workflows is not None:
                workflows.dispatch_key_repeat(**repeat_args)
            else:
                self._ensure_action_dispatcher().dispatch_key_repeat(**repeat_args)
        elif action == keys.ACTION_RELEASE:
            self._keys_down.discard(key)

    key_event = on_key_event


    def on_focus_event(self, focused: bool):
        # On focus loss/gain, clear transient pressed/captured state so a
        # missed release event cannot leave controls unresponsive.
        self._reset_transient_input_state("focus change")

        # Fallback for platforms where iconify callback isn't reliable:
        # if focus is lost, pause; if focus returns and window is not
        # actually minimized, resume.
        if not focused:
            self._set_background_pause(True, "focus lost")
        else:
            self._set_background_pause(self._query_runtime_iconified_state(), "focus gained")

    focus_event = on_focus_event

    def on_iconify_event(self, iconified: bool):
        # Minimize/restore paths can behave similarly to focus changes.
        self._set_background_pause(bool(iconified), "window iconified")

    iconify_event = on_iconify_event


    def on_mouse_position_event(self, x, y, dx, dy):
        self._handle_mouse_look_motion(x, y, dx, dy)

    mouse_position_event = on_mouse_position_event

    def on_mouse_drag_event(self, x, y, dx, dy):
        # Win32 can dispatch a drag before the native window's Python-side
        # controls have completed construction. Do not dereference the overlay
        # until initialization has established it.
        if (
            not getattr(self, "_window_setup_complete", False)
            or self._input_is_suppressed()
        ):
            return
        if self.controls_overlay.is_waiting_for_begin:
            return
        self._handle_mouse_look_motion(x, y, dx, dy)

    mouse_drag_event = on_mouse_drag_event


    def on_mouse_press_event(self, x, y, button):
        """Normalize, resolve, and apply one backend pointer press."""
        self._apply_pointer_press_intent(
            self._pointer_press_intent(button),
            x=x,
            y=y,
        )

    mouse_press_event = on_mouse_press_event


    def on_mouse_release_event(self, x, y, button):
        """Apply one normalized backend pointer release."""
        del x, y
        kind = self._pointer_release_kind(button)
        if kind is viewer_input.PointerReleaseKind.IGNORE:
            return
        if kind is viewer_input.PointerReleaseKind.STOP_OPTION_LOOK:
            self._mouse_look_left_option_active = False
            self._mouse_look_active = False
            self.wnd.mouse_exclusivity = False
            return
        if kind is viewer_input.PointerReleaseKind.STOP_MOUSE_LOOK:
            self._mouse_look_active = False
            self.wnd.mouse_exclusivity = False
            return
        self.color_picker.on_mouse_release()

    mouse_release_event = on_mouse_release_event

    def on_mouse_scroll_event(self, x_offset, y_offset):
        if self._input_is_suppressed():
            return
        if self._recorded_dive_is_paused():
            return
        camera = getattr(self, "camera", None)
        if camera is None:
            return
        camera.adjust_speed(y_offset)

    mouse_scroll_event = on_mouse_scroll_event

    def _cancel_active_import(self) -> None:
        self._ensure_import_controller().cancel_active_import()

    def _shutdown_active_import(self) -> None:
        self._ensure_import_controller().shutdown()

    def _hide_window_before_close(self) -> None:
        """Remove the native viewer before releasing its visible GL surface."""
        window = getattr(self, "wnd", None)
        if window is None:
            return
        try:
            window.visible = False
        except Exception:
            # Backends without a visibility property still retain the regular
            # close path below; this is a presentation-only improvement.
            pass

    def _complete_window_close(self) -> None:
        """Release viewer resources after any active capture has finished."""
        workflows = self.__dict__.get("_workflow_coordinator")
        if workflows is not None:
            if not workflows.begin_shutdown():
                return
        elif self._closing_requested:
            return
        self._closing_requested = True
        if workflows is None:
            self._ensure_capture_workflow().complete_close_workflows()
        try:
            self._slice_reveal_before_close = False
            self._slice_reveal_output_path = None

            if hasattr(self, "wnd"):
                try:
                    self.wnd.mouse_exclusivity = False
                except Exception:
                    pass

            if getattr(self, "_import_active", False):
                self._shutdown_active_import()

            self._finish_benchmark(reason="viewer_closed")

            if self._has_map_loaded:
                self._teardown_current_map(final_shutdown=True)
            self._release_window_resources()

            # Ensure the backend window loop receives an explicit close request.
            if hasattr(self, "wnd") and hasattr(self.wnd, "close"):
                try:
                    self.wnd.close()
                except Exception:
                    pass
        finally:
            if workflows is not None:
                workflows.complete_shutdown()

    def on_close(self):
        if self._closing_requested:
            return
        if self._ensure_import_controller().request_pause_for_close():
            self._defer_backend_close_request()
            return
        if self._escape_capture_cancellation_active():
            # Escape owns this shutdown request until discard cleanup and the
            # three-second no-save confirmation have both completed.
            self._defer_backend_close_request()
            return
        if self._exit_capture_finalization_active():
            # Repeated close requests must not tear down the OpenGL context
            # while the non-daemon writer is publishing the user's file.
            self._defer_backend_close_request()
            return

        slice_selection = self._ensure_slice_selection_controller()
        if slice_selection.countdown_active:
            # Match recording/trace behavior: an armed countdown has not
            # captured a user artifact, so closing simply disarms it.
            slice_selection.cancel_countdown()
            self._clear_slice_context()

        artifact_names = self._exit_capture_artifacts()
        if artifact_names:
            self._begin_exit_capture_finalization(artifact_names)
            return

        self._complete_window_close()

    close = on_close


def _run_moderngl_window_config(config_class: type, args=None) -> None:
    """Run the native loop through the launch boundary."""
    viewer_window_launch.run_moderngl_window_config(
        config_class,
        args,
        window_api=mglw,
        stage_recorder=record_runtime_stage,
        exception_recorder=record_runtime_exception,
        logger=_LOG,
    )


def _session_window_config_class(
    session: ViewerSession,
    *,
    window_size: tuple[int, int],
) -> type[CaveViewerWindow]:
    """Bind one immutable session to the class-based ModernGL launch API."""
    return viewer_window_launch.session_window_config_class(
        session,
        window_class=CaveViewerWindow,
        window_size=window_size,
        module_name=__name__,
    )


def _launch_viewer_window(
    session: ViewerSession,
    *,
    window_size_override: tuple[int, int] | None = None,
) -> None:
    """Launch one session through the selected native backend."""
    viewer_window_launch.launch_viewer_window(
        session,
        window_size_override=window_size_override,
        default_window_size=_DEFAULT_WINDOW_SIZE,
        desktop_window_scale=_DESKTOP_WINDOW_SCALE,
        presentation_profile=_presentation_profile_for_runtime(
            session.config.platform_runtime
        ),
        desktop_relative_window_size=_desktop_relative_window_size,
        launch_preflight=viewer_launch_preflight,
        authorize_launch_target=authorized_viewer_launch_target,
        config_class_factory=_session_window_config_class,
        runner=_run_moderngl_window_config,
        window_backend_adapter_factory=lambda: _window_backend_adapter_for_runtime(
            session.config.platform_runtime
        ),
        stage_recorder=record_runtime_stage,
        exception_recorder=record_runtime_exception,
    )


def _normalize_map_root(
    map_root: str | os.PathLike[str] | None,
) -> str | None:
    """Return an absolute map root, or ``None`` when launch context lacks one."""
    if map_root is None:
        return None
    raw_map_root = os.fspath(map_root).strip()
    if not raw_map_root:
        return None
    return os.path.abspath(os.path.expanduser(raw_map_root))


def run_viewer(
    cache_dir: str,
    textures_dir: str,
    recorded_dive_trace: recorded_dive.RecordedDiveTrace | None = None,
    platform_runtime: PlatformRuntime | None = None,
    runtime_settings: RuntimeSettings | None = None,
    map_root: str | os.PathLike[str] | None = None,
):
    manifest = chunker.load_manifest(cache_dir)
    session = ViewerSession(
        ViewerSessionConfig(
            mode=ViewerLaunchMode.READY_CACHE,
            cache_dir=cache_dir,
            textures_dir=textures_dir,
            map_root=_normalize_map_root(map_root),
            manifest=manifest,
            recorded_dive_trace=recorded_dive_trace,
            platform_runtime=platform_runtime,
            runtime_settings=runtime_settings,
            vsync=(
                runtime_settings.viewer_configuration().vsync
                if runtime_settings is not None
                else _env_bool("CAVEVIEWER_VSYNC", True)
            ),
        )
    )

    try:
        _launch_viewer_window(session)
    finally:
        bitmap_font.clear_runtime_style()


def _cache_manifest_sha256(cache_dir: str) -> str:
    manifest_path = os.path.join(cache_dir, chunker.MANIFEST_NAME)
    digest = hashlib.sha256()
    with open(manifest_path, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def run_viewer_benchmark(
    cache_dir: str,
    textures_dir: str,
    scenario,
    output_dir: str,
    *,
    runtime_settings: RuntimeSettings | None = None,
):
    """Run a deterministic viewer benchmark against an existing chunk cache."""
    import platform as _platform

    summary_path = os.path.join(output_dir, "summary.json")
    manifest = chunker.load_manifest(cache_dir)
    streaming_settings = _benchmark_streaming_settings_snapshot(
        scenario,
        runtime_settings=runtime_settings,
    )
    streaming_fingerprint = _benchmark_streaming_settings_fingerprint(
        streaming_settings
    )
    viewer_settings = (
        runtime_settings.viewer_configuration()
        if runtime_settings is not None
        else None
    )
    benchmark_platform_runtime = None
    if runtime_settings is not None:
        from caveviewer.gui.platform.runtime import create_platform_runtime

        benchmark_platform_runtime = create_platform_runtime(
            runtime_settings=runtime_settings
        )
    benchmark_config = ViewerBenchmarkConfig(
        scenario=scenario,
        output_dir=output_dir,
        environment={
            "app_version": APP_VERSION,
            "python": sys.version.split()[0],
            "platform": _platform.platform(),
            "cache_dir": os.path.abspath(cache_dir),
            "textures_dir": os.path.abspath(textures_dir),
            "cache_manifest_sha256": _cache_manifest_sha256(cache_dir),
            "scenario": scenario.name,
            "scenario_fingerprint": scenario.fingerprint,
            "source_sha": os.environ.get("GITHUB_SHA")
            or (
                viewer_settings.commit_identifier
                if viewer_settings is not None
                else os.environ.get("CAVEVIEWER_COMMIT", "")
            ),
            "vsync_env": (
                str(viewer_settings.vsync).lower()
                if viewer_settings is not None
                else os.environ.get("CAVEVIEWER_VSYNC", "")
            ),
            "streaming_settings": streaming_settings,
            "streaming_settings_fingerprint": streaming_fingerprint,
            "render_distance_chunks": streaming_settings["render_distance_chunks"],
            "memory_target_percent": streaming_settings["system_ram_target_percent"],
            "gpu_memory_target_percent": streaming_settings[
                "gpu_memory_target_percent"
            ],
            "gpu_memory_override_gb": streaming_settings["gpu_memory_override_gb"],
            "io_workers": streaming_settings["io_workers"],
            "io_reserved_cpus": streaming_settings["io_reserved_cpus"],
            "upload_chunks_per_frame": streaming_settings[
                "upload_chunks_per_frame"
            ],
            "upload_groups_per_frame": streaming_settings[
                "upload_groups_per_frame"
            ],
            "upload_time_budget_ms": streaming_settings["upload_time_budget_ms"],
        },
    )
    session = ViewerSession(
        ViewerSessionConfig(
            mode=ViewerLaunchMode.BENCHMARK,
            cache_dir=cache_dir,
            textures_dir=textures_dir,
            manifest=manifest,
            benchmark=benchmark_config,
            platform_runtime=benchmark_platform_runtime,
            runtime_settings=runtime_settings,
            vsync=(
                viewer_settings.vsync
                if viewer_settings is not None
                else _env_bool("CAVEVIEWER_VSYNC", True)
            ),
        )
    )

    try:
        _launch_viewer_window(session)
        return summary_path
    finally:
        bitmap_font.clear_runtime_style()


def run_viewer_with_pending_import(
    model_descriptor: dict,
    textures_dir: str,
    recorded_dive_trace: recorded_dive.RecordedDiveTrace | None = None,
    platform_runtime: PlatformRuntime | None = None,
    runtime_settings: RuntimeSettings | None = None,
):
    """
    Launches the viewer window for a map that needs FIRST-TIME import
    (no generated cache yet) -- used by caveviewer.app's main() instead
    of run_viewer() specifically so the import can run AFTER the window
    is open, showing real progress in the same in-window panel the OPEN
    button already uses, rather than the old behavior of running the
    import entirely before any window existed (which could only show a
    plain console progress bar, with nowhere graphical to draw into yet).

    model_descriptor is whatever caveviewer.app's find_model_file()
    returned -- a small dict identifying which format (.obj, .glb)
    and the relevant file path(s), format-agnostic so this single
    function/code path covers every supported source format rather than
    needing a separate pending-import entry point per format.

    The window opens immediately with no map loaded; the actual import
    is triggered from inside CaveViewerWindow.on_render()'s first frame
    (see _run_pending_import) once the window is confirmed to have
    rendered and is genuinely on screen.
    """
    session = ViewerSession(
        ViewerSessionConfig(
            mode=ViewerLaunchMode.PENDING_IMPORT,
            pending_import=PendingImportRequest(
                model_descriptor=model_descriptor,
                textures_dir=textures_dir,
            ),
            recorded_dive_trace=recorded_dive_trace,
            platform_runtime=platform_runtime,
            runtime_settings=runtime_settings,
            vsync=(
                runtime_settings.viewer_configuration().vsync
                if runtime_settings is not None
                else _env_bool("CAVEVIEWER_VSYNC", True)
            ),
        )
    )

    try:
        _launch_viewer_window(session)
    except BaseException as error:
        outcome = session.outcome
        # Some native backends surface a programmatic window close as
        # SystemExit. Suppress it only after the import controller has recorded
        # the recoverable startup failure that requested that close.
        if isinstance(error, SystemExit) and outcome.kind == "import_failed":
            _LOG.info("Viewer returned to the library after startup import failure.")
            return outcome
        # Suppress the known "no initial map" runtime error that can occur
        # when the viewer is launched without a preloaded map and the GUI
        # is closed; let other RuntimeErrors propagate.
        msg = str(error)
        if isinstance(error, RuntimeError) and (
            "viewer session has neither a ready cache" in msg.lower()
        ):
            # Clean exit without a traceback
            _LOG.info("Viewer exited without a preloaded map.")
            return outcome
        raise
    else:
        return session.outcome
    finally:
        bitmap_font.clear_runtime_style()
