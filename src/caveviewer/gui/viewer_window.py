"""OpenGL viewer-window lifecycle and render-loop orchestration.

The actual OpenGL window: owns the moderngl context, the free-fly camera,
the StreamingWorld (which decides what to load/unload), and the per-chunk
GPU buffers/textures. This is where the rest of caveviewer.core and caveviewer.gui
gets wired together into a runnable program.

Each loaded chunk becomes a small set of moderngl VAOs, one per material
group within that chunk (so each can be drawn with its own bound texture).
We keep a dict: cell -> list[(vao, texture_material_name)] so unload is a
simple lookup-and-release.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
import logging
import math
import os
import queue
import sys
import threading
import time

import numpy as np
import moderngl
import moderngl_window as mglw
from moderngl_window.context.base import KeyModifiers

from caveviewer.core.chunking import builder as chunker
from caveviewer.core.hardware import gpu_memory, memory_targets, system_memory
from caveviewer.core.diagnostics.logging import get_logger
from caveviewer.core.streaming.world import StreamingWorld, StreamingConfig
from caveviewer.gui.texture_manager import TextureManager
from caveviewer.gui.camera import FlyCamera
from caveviewer.gui.minimap import Minimap
from caveviewer.gui.render_mode_buttons import RenderModeButtons
from caveviewer.gui.controls_overlay import ControlsOverlay
from caveviewer.gui.stepper_control import StepperControl
from caveviewer.gui.color_picker import ColorPicker
from caveviewer.gui.import_progress_panel import ImportProgressPanel
from caveviewer.gui.import_process import (
    start_import_process,
    terminate_import_process,
)
from caveviewer.gui.import_controller import MapImportController
from caveviewer.gui.map_opening import pick_folder_dialog, resolve_selected_map_folder
from caveviewer.gui import recording
from caveviewer.gui import bitmap_font
from caveviewer.gui import render_upload
from caveviewer.gui import viewer_bookmarks
from caveviewer.gui.recording_controller import RecordingStateController
from caveviewer.gui.platform.factory import get_platform_adapter
from caveviewer.gui.platform import tk_root_options
from caveviewer.gui.platform.windowing import run_window_config
from caveviewer.resources import image_path, resource_path
from caveviewer.version import APP_NAME, APP_VERSION

_LOG = get_logger("CaveViewer")

_DEFAULT_WINDOW_SIZE = (1600, 1000)
_DESKTOP_WINDOW_SCALE = 0.80
_VIEWER_UI_BASE_WINDOW_SIZE = (1536, 864)
_VIEWER_UI_SCALE_ENV = "CAVEVIEWER_VIEWER_UI_SCALE"
_VIEWER_UI_SCALE_MAX = 1.45
_GPU_RESIDENCY_SAFETY_SHARE = 0.05
_RENDER_UPLOAD_VERTEX_BYTES = render_upload.RENDER_UPLOAD_VERTEX_BYTES
_RENDER_UPLOAD_INITIAL_SLICE_BYTES = render_upload.RENDER_UPLOAD_INITIAL_SLICE_BYTES
_RENDER_UPLOAD_SLICE_BYTES = _RENDER_UPLOAD_INITIAL_SLICE_BYTES
_CATCHUP_UPLOAD_CHUNKS_PER_FRAME = 2
_CATCHUP_UPLOAD_OPERATIONS_PER_CHUNK = 8
_CATCHUP_UPLOAD_TIME_BUDGET_MS = 8.0
_STARTUP_UPLOAD_CHUNKS_PER_FRAME = 4
_STARTUP_UPLOAD_OPERATIONS_PER_CHUNK = 8
_STARTUP_UPLOAD_TIME_BUDGET_MS = 12.0
_VIEWER_STREAMING_SHUTDOWN_TIMEOUT_SECONDS = 2.0
_ICONIFIED_RENDER_POLL_INTERVAL_S = 0.12
_IMPORT_PAUSE_NOTICE_RENDER_INTERVAL_S = 1.0 / 30.0


_RecordingStopResult = recording.RecordingStopResult
_RecordingReadbackSlot = recording.RecordingReadbackSlot


def _import_controller_property(attribute_name: str):
    def getter(self):
        return getattr(self._ensure_import_controller(), attribute_name)

    def setter(self, value) -> None:
        setattr(self._ensure_import_controller(), attribute_name, value)

    return property(getter, setter)


def _tk_root_exists(root) -> bool:
    """Return whether a Tk root-like object is still usable."""
    if root is None:
        return False
    try:
        return bool(root.winfo_exists())
    except Exception:
        return False


def _screen_size_from_tk_root(root) -> tuple[int, int] | None:
    """Read a positive desktop size from a Tk root-like object."""
    try:
        desktop_width = int(root.winfo_screenwidth())
        desktop_height = int(root.winfo_screenheight())
    except Exception:
        return None
    if desktop_width <= 0 or desktop_height <= 0:
        return None
    return desktop_width, desktop_height


def _window_size_from_desktop_size(desktop_size: tuple[int, int]) -> tuple[int, int]:
    """Return CaveViewer's default viewer size for a detected desktop."""
    desktop_width, desktop_height = desktop_size
    if desktop_width <= 0 or desktop_height <= 0:
        return _DEFAULT_WINDOW_SIZE

    window_size = (
        max(1, int(round(desktop_width * _DESKTOP_WINDOW_SCALE))),
        max(1, int(round(desktop_height * _DESKTOP_WINDOW_SCALE))),
    )
    _LOG.info(
        "Desktop size %dx%d; opening viewer at %dx%d.",
        desktop_width, desktop_height, *window_size,
    )
    return window_size


def _desktop_relative_window_size(screen_source=None) -> tuple[int, int]:
    """
    Return an 80%-of-screen fallback for non-GLFW desktop backends.

    When a Tk root already exists, reuse it for screen-size measurement instead
    of creating a second Tk application root.  This matters most on macOS,
    where the kept-alive splash root owns process-level Tk app/menu state.
    """
    if screen_source is not None:
        screen_size = _screen_size_from_tk_root(screen_source)
        if screen_size is None:
            _LOG.warning(
                "Could not detect desktop size from existing Tk root; using %dx%d.",
                *_DEFAULT_WINDOW_SIZE,
            )
            return _DEFAULT_WINDOW_SIZE
        return _window_size_from_desktop_size(screen_size)

    root = None
    owns_root = False
    try:
        import tkinter as tk

        default_root = getattr(tk, "_default_root", None)
        if _tk_root_exists(default_root):
            screen_size = _screen_size_from_tk_root(default_root)
            if screen_size is None:
                _LOG.warning(
                    "Could not detect desktop size from existing Tk root; using %dx%d.",
                    *_DEFAULT_WINDOW_SIZE,
                )
                return _DEFAULT_WINDOW_SIZE
            return _window_size_from_desktop_size(screen_size)

        root = tk.Tk(**tk_root_options())
        owns_root = True
        root.withdraw()
        screen_size = _screen_size_from_tk_root(root)
        if screen_size is None:
            return _DEFAULT_WINDOW_SIZE
        return _window_size_from_desktop_size(screen_size)
    except Exception as e:
        _LOG.warning("Could not detect desktop size (%s); using %dx%d.", e, *_DEFAULT_WINDOW_SIZE)
        return _DEFAULT_WINDOW_SIZE
    finally:
        if owns_root and root is not None:
            try:
                root.destroy()
            except Exception:
                pass


def _window_pixel_ratio(window) -> float:
    """Return framebuffer pixels per logical window pixel for crisp UI text."""
    try:
        width, height = window.size
        buffer_width, buffer_height = window.buffer_size
        width = max(1, int(width))
        height = max(1, int(height))
        return max(1.0, min(4.0, max(buffer_width / width, buffer_height / height)))
    except Exception:
        return 1.0


def _viewer_ui_scale_for_window_size(
    window_size: tuple[int, int] | None,
    environ: Mapping[str, str] | None = None,
) -> float:
    """Return an automatic HUD scale for the current viewer surface.

    The control overlay is rendered inside OpenGL, so it does not inherit GNOME
    titlebar or XWayland desktop scaling.  Keep the old compact size at the
    1536x864 default viewer window, then grow the HUD on larger viewer surfaces.
    The environment override is for development/testing; the normal user path
    is automatic.
    """
    environment = os.environ if environ is None else environ
    raw_override = str(environment.get(_VIEWER_UI_SCALE_ENV, "")).strip()
    if raw_override:
        try:
            return max(0.75, min(2.0, float(raw_override)))
        except ValueError:
            pass

    try:
        width, height = window_size or _DEFAULT_WINDOW_SIZE
        width = max(1, int(width))
        height = max(1, int(height))
    except Exception:
        width, height = _DEFAULT_WINDOW_SIZE

    base_width, base_height = _VIEWER_UI_BASE_WINDOW_SIZE
    size_scale = min(width / base_width, height / base_height)
    return max(1.0, min(_VIEWER_UI_SCALE_MAX, size_scale))


def _map_import_inhibit_reason(map_name: str) -> str:
    """Return the desktop-visible reason used while importing a map."""
    display_name = str(map_name or "").strip() or "map"
    return f"Importing {display_name}"


def _acquire_map_import_inhibitor(map_name: str):
    """Best-effort desktop idle/suspend inhibitor for long map imports."""
    try:
        from caveviewer.gui.platform import get_desktop_services

        return get_desktop_services().inhibit_idle_suspend(
            _map_import_inhibit_reason(map_name)
        )
    except Exception as exc:
        # Desktop integration must not block opening maps. Linux portals
        # provide the real inhibitor; unsupported sessions continue normally.
        _LOG.warning(
            "Desktop idle/suspend inhibit unavailable during map import: %s",
            exc,
        )
        return None


def _release_desktop_inhibitor(inhibitor) -> None:
    """Release a desktop inhibitor without affecting import completion."""
    if inhibitor is None:
        return
    try:
        inhibitor.close()
    except Exception as exc:
        _LOG.warning(
            "Desktop idle/suspend inhibit release failed after map import: %s",
            exc,
        )


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


SHADER_DIR = str(resource_path("shaders"))


def _runtime_app_icon_path() -> str:
    filenames = (get_platform_adapter().splash_layout_policy().app_icon_resource_name,)
    for filename in filenames:
        path = image_path(filename)
        if path.exists():
            return str(path)
    return str(image_path(filenames[0]))


APP_ICON_PATH = _runtime_app_icon_path()


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


class CaveViewerWindow(mglw.WindowConfig):
    gl_version = (3, 3)
    title = APP_NAME
    # The launch helpers replace this fallback with an 80%-of-desktop size.
    # Keep aspect_ratio unlocked so manual resizing remains fully flexible.
    window_size = _DEFAULT_WINDOW_SIZE
    resizable = True
    # Allow disabling vsync via env var -- useful on VMs where the virtual
    # display driver can block swap_buffers() long enough to freeze the
    # render thread and make the window appear hung during heavy imports.
    vsync = os.environ.get("CAVEVIEWER_VSYNC", "1").strip() not in ("0", "false", "no")
    aspect_ratio = None  # don't letterbox; we recompute from actual window size

    # Set on the class itself (not passed through __init__ kwargs) before
    # calling mglw.run_window_config(). Different moderngl-window versions
    # have changed how/whether run_window_config forwards extra keyword
    # arguments into WindowConfig.__init__, so relying on that passthrough
    # is fragile across versions. Class attributes are a stable mechanism
    # regardless of moderngl-window's internal arg handling -- run_viewer()
    # at the bottom of this file sets these right before launching.
    cave_cache_dir: str = None
    cave_textures_dir: str = None
    cave_manifest: dict = None

    # Alternative to the three attributes above: set THIS instead when the
    # map needs first-time import/chunking (no cache built yet) -- a dict
    # with keys "obj_path", "mtl_path", "textures_dir". When set, the
    # window opens immediately with no map loaded, and the actual import
    # runs from inside on_render()'s first frame (see _run_pending_import),
    # so the existing in-window ImportProgressPanel can show real progress
    # the same way it already does for the OPEN button's mid-session
    # imports -- rather than the old behavior of running the import
    # entirely before any window existed, which could only show a plain
    # console progress bar with nowhere graphical to draw into yet.
    cave_pending_import: dict = None

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
    RIGHT_COLUMN_PANEL_LABEL_SIZE = 1.7
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
    RECORDING_READBACK_BUFFER_COUNT = 3
    RECORDING_READBACK_COMPONENTS = 3
    RECORDING_RAW_PIX_FMT = "rgb24"

    # Startup focus forcing can make bundled macOS app windows appear in a
    # corner first and then jump as the window manager re-places them.
    # Default to disabled for frozen macOS builds; allow override.
    FORCE_STARTUP_FOCUS_ENV = "CAVEVIEWER_FORCE_STARTUP_FOCUS"

    _import_active = _import_controller_property("active")
    _import_is_startup = _import_controller_property("is_startup")
    _import_thread = _import_controller_property("thread")
    _import_process = _import_controller_property("process")
    _import_command_queue = _import_controller_property("command_queue")
    _import_cache_dir = _import_controller_property("cache_dir")
    _import_stop_event = _import_controller_property("stop_event")
    _import_queue = _import_controller_property("event_queue")
    _import_pause_requested = _import_controller_property("pause_requested")
    _import_model_format = _import_controller_property("model_format")
    _import_map_name = _import_controller_property("map_name")
    _import_progress_stage = _import_controller_property("progress_stage")
    _import_progress_fraction = _import_controller_property("progress_fraction")
    _import_progress_title = _import_controller_property("progress_title")
    _import_progress_note = _import_controller_property("progress_note")
    _import_resuming_from_checkpoint = _import_controller_property(
        "resuming_from_checkpoint"
    )
    _import_pause_notice_until = _import_controller_property("pause_notice_until")
    _import_pause_notice_close_after = _import_controller_property(
        "pause_notice_close_after"
    )
    _import_pause_notice_map_name = _import_controller_property("pause_notice_map_name")
    _import_pause_notice_title = _import_controller_property("pause_notice_title")
    _import_pause_notice_stage = _import_controller_property("pause_notice_stage")
    _import_pause_notice_note = _import_controller_property("pause_notice_note")

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._window_setup_complete = False
        self._platform_adapter = get_platform_adapter()
        self._set_runtime_window_icon()

        force_focus_env = os.getenv(self.FORCE_STARTUP_FOCUS_ENV, "").strip().lower()
        force_focus = force_focus_env in {"1", "true", "yes", "on"}
        self._startup_focus_enabled = True
        if self._platform_adapter.suppress_forced_startup_focus(
            is_frozen=bool(getattr(sys, "frozen", False)),
            force_requested=force_focus,
        ):
            self._startup_focus_enabled = False

        # Optional env override for quick testing/tuning without code edits.
        text_scale_env = os.getenv("CAVEVIEWER_UI_TEXT_SCALE")
        if text_scale_env:
            try:
                bitmap_font.set_text_scale(float(text_scale_env))
            except ValueError:
                bitmap_font.set_text_scale(self.UI_TEXT_SCALE)
        else:
            bitmap_font.set_text_scale(self.UI_TEXT_SCALE)
        bitmap_font.set_raster_scale(_window_pixel_ratio(getattr(self, "wnd", None)))
        self._viewer_ui_scale = _viewer_ui_scale_for_window_size(
            getattr(getattr(self, "wnd", None), "size", _DEFAULT_WINDOW_SIZE)
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

        have_ready_cache = CaveViewerWindow.cave_cache_dir is not None
        have_pending_import = CaveViewerWindow.cave_pending_import is not None

        if not have_ready_cache and not have_pending_import:
            raise RuntimeError(
                "Neither CaveViewerWindow.cave_cache_dir nor .cave_pending_import "
                "was set before launch. One or the other must be set by "
                "run_viewer() / run_viewer_with_pending_import() before "
                "constructing this window."
            )

        self.import_progress_panel = None
        self._pending_import_splash_rendered = False
        if have_pending_import:
            self.import_progress_panel = ImportProgressPanel(self.ctx)
            self._pending_import_splash_rendered = (
                self._present_pending_import_splash_now()
            )

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
        self._gpu_draw_timer_enabled = _env_bool("CAVEVIEWER_GPU_DRAW_TIMER", False)
        self._streaming_frame_timing: dict | None = None
        self._last_input_reset_log = 0.0
        self._layout_cache_size: tuple | None = None
        self._layout_cache_result: dict | None = None
        self._is_iconified = False
        self._is_background_paused = False
        self._render_throttle_due_at: dict[str, float] = {}
        self._closing_requested = False
        self._startup_focus_requested = False
        self._upload_chunks_per_frame = _env_int("CAVEVIEWER_UPLOAD_CHUNKS_PER_FRAME", 1, 1, 16)
        self._upload_groups_per_frame = _env_int("CAVEVIEWER_UPLOAD_GROUPS_PER_FRAME", 1, 1, 64)
        self._upload_time_budget_ms = _env_float("CAVEVIEWER_UPLOAD_TIME_BUDGET_MS", 3.0, 0.5, 50.0)
        self._current_upload_operations_per_chunk = self._upload_groups_per_frame
        self._current_upload_time_budget_ms = self._upload_time_budget_ms
        self._vbo_upload_slice_bytes = _RENDER_UPLOAD_INITIAL_SLICE_BYTES
        self._texture_upload_slice_bytes = _RENDER_UPLOAD_INITIAL_SLICE_BYTES
        self._navigation_guard_enabled = _env_bool("CAVEVIEWER_NAVIGATION_GUARD", True)
        self._navigation_guard_radius_cells = _env_int("CAVEVIEWER_NAVIGATION_GUARD_RADIUS_CELLS", 2, 0, 12)
        self._bookmarks_path: str | None = None
        self._bookmarks: viewer_bookmarks.BookmarkSlots = {}
        self._recording_fps = _env_int("CAVEVIEWER_RECORDING_FPS", 30, 1, 60)
        self._recording_max_height = _env_int("CAVEVIEWER_RECORDING_MAX_HEIGHT", 1080, 240, 4320)
        self._recording_crf = _env_int("CAVEVIEWER_RECORDING_CRF", 23, 0, 51)
        self._recording_output_dir = os.path.expanduser(
            os.getenv("CAVEVIEWER_RECORDING_DIR", os.path.join("~", "Movies", "CaveViewer"))
        )
        self._recording_controller = RecordingStateController(
            frame_interval=1.0 / float(self._recording_fps)
        )
        self._recording_session: recording.RecordingEncoderSession | None = None
        self._recording_output_path: str | None = None
        self._recording_size: tuple[int, int] | None = None
        self._recording_viewport: tuple[int, int, int, int] | None = None
        self._recording_readback_framebuffer: moderngl.Framebuffer | None = None
        self._recording_readback_slots: list[_RecordingReadbackSlot] = []
        self._recording_readback_pending: list[_RecordingReadbackSlot] = []
        self._recording_readback_byte_count = 0
        self._recording_frame_queue: queue.Queue | None = None
        self._recording_stop_results: queue.Queue[_RecordingStopResult] = queue.Queue()
        self._recording_stop_thread: threading.Thread | None = None

        self._install_backend_modifier_probe()

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
        self.controls_overlay = ControlsOverlay(self.ctx)
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
            self.import_progress_panel = ImportProgressPanel(self.ctx)
        self.controls_overlay.set_logo_renderer(self.import_progress_panel)

        self.ctx.enable(moderngl.DEPTH_TEST)
        self.ctx.enable(moderngl.CULL_FACE)

        # Map-specific state (world, manifest, camera, minimap, texture manager,
        # chunk GPU objects) lives in its own method, separate
        # from the one-time-per-window setup above, so the exact same
        # logic can run again later when switching to a different map via
        # the OPEN button -- see load_new_map() / _teardown_current_map().
        self.cache_dir = None
        self.textures_dir = None
        self.manifest = None
        self.world = None
        self.camera = None
        self.minimap = None
        self.texture_manager = None
        self._chunk_gpu_objects: dict[tuple, list] = {}
        self._chunk_upload_states: dict[tuple, dict] = {}
        # Per-chunk, per-material CPU-side data for instant SHADE toggle:
        # each entry holds (mat_name, positions, uvs, smooth_normals, flat_normals)
        # tuples in the same order as _chunk_gpu_objects, so toggling shading
        # can zip the two lists and rewrite each VBO in place via vbo.write().
        self._chunk_normal_cache: dict[tuple, list] = {}
        # Per-cell world-space AABBs for frustum culling, populated in
        # _load_map from the manifest's pre-computed bounding boxes.
        self._chunk_aabbs: dict[tuple, tuple] = {}
        self._navigation_guard_cells: set[tuple[int, int, int]] = set()
        self._navigation_guard_chunk_size: float | None = None
        self._has_map_loaded = False
        self._pending_import_started = False
        self._initial_chunks_loaded = False
        self._initial_compilation_started_at = None
        self._initial_compilation_logged = False
        self._chunk_prep_progress = 0.0
        self._chunk_prep_complete_until = None
        self._chunk_prep_completion_armed = False
        self._window_resources_released = False

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

        if have_ready_cache:
            self._load_map(
                CaveViewerWindow.cave_cache_dir,
                CaveViewerWindow.cave_textures_dir,
                CaveViewerWindow.cave_manifest,
            )
            self._has_map_loaded = True
        # else: have_pending_import is true instead -- the actual import
        # is deliberately NOT run here, before the window has rendered
        # even one frame. It's triggered from inside on_render() instead
        # (see _run_pending_import), once the window is confirmed to
        # actually be open and able to draw the in-window progress panel
        # -- starting the blocking import here, before super().__init__()
        # has truly finished and the window is on screen, would risk the
        # exact same "nothing to draw into yet" problem this feature
        # exists to avoid.
        self._window_setup_complete = True

    def _active_platform_adapter(self):
        """Return the initialized platform adapter, creating it for test shells."""
        adapter = getattr(self, "_platform_adapter", None)
        if adapter is None:
            adapter = get_platform_adapter()
            self._platform_adapter = adapter
        return adapter

    def _ensure_import_controller(self) -> MapImportController:
        controller = self.__dict__.get("_import_controller")
        if controller is None:
            controller = MapImportController(
                self,
                logger=lambda: _LOG,
                chunker=lambda: chunker,
                start_import_process=lambda: start_import_process,
                terminate_import_process=lambda: terminate_import_process,
                acquire_inhibitor=lambda: _acquire_map_import_inhibitor,
                release_inhibitor=lambda: _release_desktop_inhibitor,
                perf_counter=lambda: time.perf_counter(),
                monotonic=lambda: time.monotonic(),
            )
            self.__dict__["_import_controller"] = controller
        return controller

    def _ensure_recording_controller(self) -> RecordingStateController:
        controller = self.__dict__.get("_recording_controller")
        if controller is None:
            controller = RecordingStateController()
            self.__dict__["_recording_controller"] = controller
        return controller

    @property
    def _recording_countdown_started_at(self) -> float | None:
        return self._ensure_recording_controller().countdown_started_at

    @_recording_countdown_started_at.setter
    def _recording_countdown_started_at(self, value: float | None) -> None:
        self._ensure_recording_controller().countdown_started_at = value

    @property
    def _recording_countdown_until(self) -> float | None:
        return self._ensure_recording_controller().countdown_until

    @_recording_countdown_until.setter
    def _recording_countdown_until(self, value: float | None) -> None:
        self._ensure_recording_controller().countdown_until = value

    @property
    def _recording_last_stage_ms(self) -> float:
        return self._ensure_recording_controller().last_stage_ms

    @_recording_last_stage_ms.setter
    def _recording_last_stage_ms(self, value: float) -> None:
        self._ensure_recording_controller().last_stage_ms = value

    @property
    def _recording_last_drain_ms(self) -> float:
        return self._ensure_recording_controller().last_drain_ms

    @_recording_last_drain_ms.setter
    def _recording_last_drain_ms(self, value: float) -> None:
        self._ensure_recording_controller().last_drain_ms = value

    @property
    def _recording_next_frame_time(self) -> float | None:
        return self._ensure_recording_controller().next_frame_time

    @_recording_next_frame_time.setter
    def _recording_next_frame_time(self, value: float | None) -> None:
        self._ensure_recording_controller().next_frame_time = value

    @property
    def _recording_frame_interval(self) -> float:
        return self._ensure_recording_controller().frame_interval

    @_recording_frame_interval.setter
    def _recording_frame_interval(self, value: float) -> None:
        self._ensure_recording_controller().frame_interval = value

    @property
    def _recording_dropped_frames(self) -> int:
        return self._ensure_recording_controller().dropped_frames

    @_recording_dropped_frames.setter
    def _recording_dropped_frames(self, value: int) -> None:
        self._ensure_recording_controller().dropped_frames = value

    @property
    def _recording_status_message(self) -> str | None:
        return self._ensure_recording_controller().status_message

    @_recording_status_message.setter
    def _recording_status_message(self, value: str | None) -> None:
        self._ensure_recording_controller().status_message = value

    @property
    def _recording_status_detail(self) -> str | None:
        return self._ensure_recording_controller().status_detail

    @_recording_status_detail.setter
    def _recording_status_detail(self, value: str | None) -> None:
        self._ensure_recording_controller().status_detail = value

    @property
    def _recording_status_kind(self) -> str | None:
        return self._ensure_recording_controller().status_kind

    @_recording_status_kind.setter
    def _recording_status_kind(self, value: str | None) -> None:
        self._ensure_recording_controller().status_kind = value

    @property
    def _recording_status_until(self) -> float | None:
        return self._ensure_recording_controller().status_until

    @_recording_status_until.setter
    def _recording_status_until(self, value: float | None) -> None:
        self._ensure_recording_controller().status_until = value

    def _set_runtime_window_icon(self) -> None:
        """Set the native viewer-window icon when the backend exposes one."""
        if not os.path.exists(APP_ICON_PATH):
            _LOG.warning(f"viewer window icon asset not found: {APP_ICON_PATH}")
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
                # Try passing the path directly first — some pyglet versions
                # (and some backends) expect a filename/Path rather than a
                # pre-loaded ImageData object and will call .is_absolute() on
                # the argument, which fails on ImageData.
                set_icon(APP_ICON_PATH)
                _LOG.info("Set viewer window icon.")
                return
            except Exception:
                pass
            try:
                import pyglet
                icon = pyglet.image.load(APP_ICON_PATH)
                set_icon(icon)
                _LOG.info("Set viewer window icon.")
                return
            except Exception as e:
                _LOG.warning(f"could not set viewer window icon ({e}); continuing without it.")
                return

        _LOG.debug("viewer backend does not expose a set_icon() hook.")

    def _load_map(self, cache_dir: str, textures_dir: str, manifest: dict) -> None:
        """
        Sets up everything specific to ONE map: the texture manager, the
        streaming world, the starting camera position, and the minimap.
        Called once from __init__ for the map the program launched with,
        and called again from load_new_map() when switching to a
        different map via the OPEN button -- _teardown_current_map() must
        be called first in that second case, to cleanly release the
        previous map's GPU/thread resources before this builds new ones.
        """
        self.cache_dir = cache_dir
        self.textures_dir = textures_dir
        self.manifest = manifest
        self._initial_compilation_started_at = time.perf_counter()
        self._initial_compilation_logged = False

        gpu_vendor = str(self.ctx.info.get("GL_VENDOR", ""))
        gpu_memory_bytes = gpu_memory.detect_total_gpu_memory_bytes(
            gpu_vendor, logger=_LOG
        )
        gpu_target_fraction = memory_targets.parse_gpu_target_fraction(
            os.environ.get("CAVEVIEWER_GPU_MEMORY_UTILIZATION_TARGET")
        )
        max_texture_dimension = TextureManager.recommend_max_texture_dimension(
            self.manifest["mtl_materials"],
            gpu_memory_bytes,
            gpu_target_fraction,
        )
        ram_snapshot = system_memory.detect_ram_snapshot()
        max_decoded_cache_bytes = TextureManager.recommend_decoded_cache_bytes(
            ram_snapshot.available_bytes if ram_snapshot is not None else None
        )
        max_resident_texture_bytes = (
            TextureManager.recommend_resident_texture_cache_bytes(
                gpu_memory_bytes,
                gpu_target_fraction,
            )
        )
        gpu_geometry_budget_bytes = None
        if gpu_memory_bytes is not None and gpu_memory_bytes > 0:
            total_gpu_residency_budget_bytes = int(
                gpu_memory_bytes * gpu_target_fraction
            )
            max_resident_texture_bytes = min(
                max_resident_texture_bytes,
                total_gpu_residency_budget_bytes,
            )
            gpu_residency_safety_bytes = min(
                max(0, total_gpu_residency_budget_bytes - max_resident_texture_bytes),
                int(total_gpu_residency_budget_bytes * _GPU_RESIDENCY_SAFETY_SHARE),
            )
            gpu_geometry_budget_bytes = max(
                0,
                total_gpu_residency_budget_bytes
                - max_resident_texture_bytes
                - gpu_residency_safety_bytes,
            )
            _LOG.info(
                "GPU residency budget split: target %.1f MB (%.0f%% of %.1f GB); "
                "textures %.1f MB, geometry %.1f MB, safety %.1f MB.",
                total_gpu_residency_budget_bytes / (1024 ** 2),
                gpu_target_fraction * 100.0,
                gpu_memory_bytes / (1024 ** 3),
                max_resident_texture_bytes / (1024 ** 2),
                gpu_geometry_budget_bytes / (1024 ** 2),
                gpu_residency_safety_bytes / (1024 ** 2),
            )
        self.texture_manager = TextureManager(
            self.ctx,
            self.textures_dir,
            self.manifest["mtl_materials"],
            max_texture_dimension=max_texture_dimension,
            max_decoded_cache_bytes=max_decoded_cache_bytes,
            max_resident_texture_bytes=max_resident_texture_bytes,
        )
        self.texture_manager.validate_textures()

        def predecode_textures_for_chunk(chunk_data):
            # Called from a background worker thread (see StreamingWorld) --
            # decodes JPEGs for every material this chunk uses, ahead of
            # time, so the eventual main-thread GPU upload can use
            # already-decoded pixels rather than doing a slow
            # decode-and-upload combination.
            for group in chunk_data.groups.values():
                self.texture_manager.decode_for_material(group.material_name)

        chunk_size = chunker.manifest_chunk_size(self.manifest)
        if chunk_size is None:
            raise ValueError(
                "Map cache manifest is missing a valid chunk_size. "
                "Rebuild this map's reported cache directory with this version "
                "of CaveViewer."
            )
        configured_chunk_size = chunker.configured_chunk_size()
        _LOG.info(f"Opening map cache with manifest chunk size: {chunk_size:g}.")
        if abs(chunk_size - configured_chunk_size) > 1e-6:
            _LOG.info(
                f"Current {chunker.CHUNK_SIZE_ENV_VAR} setting is {configured_chunk_size:g}, "
                "but existing/prebuilt caches stream using the chunk size recorded in manifest.json."
            )
        config = StreamingConfig(
            chunk_size=chunk_size,
            load_radius_cells=self.render_distance_stepper.value,
            unload_radius_margin=1,
        )
        self.world = StreamingWorld(
            self.cache_dir,
            config,
            on_decode_textures=predecode_textures_for_chunk,
            prepack_smooth_shading=bool(
                self.render_mode_buttons.smooth_shading_enabled
            ),
            gpu_vendor=gpu_vendor,
            textures_dir=self.textures_dir,
            total_gpu_memory_bytes=gpu_memory_bytes,
            texture_gpu_budget_bytes=max_resident_texture_bytes,
            gpu_geometry_budget_bytes=gpu_geometry_budget_bytes,
        )

        # pick a sane starting position: center of the first available chunk,
        # so the user doesn't spawn outside the mesh and see nothing
        first_cell_str = next(iter(self.manifest["chunks"]))
        first_info = self.manifest["chunks"][first_cell_str]
        start_pos = (np.array(first_info["bounds_min"]) + np.array(first_info["bounds_max"])) / 2.0
        self.camera = FlyCamera(position=tuple(start_pos))
        self._bookmarks_path = os.path.join(self.cache_dir, "camera_bookmarks.json")
        self._load_bookmarks()

        # Bottom-left minimap: a crude top-down outline of the whole cave's
        # footprint with a live red dot for current position. Built once
        # from the manifest's chunk bounding boxes -- no extra rendering
        # pass or GPU cost beyond this tiny 2D overlay.
        self.minimap = Minimap(self.ctx, self.manifest)

        # One-time texture diagnostic: print material/texture summary to
        # console so atlas feasibility can be judged without guessing.
        self._print_texture_diagnostics(manifest, textures_dir)

        # Keep frustum-culling bounds only for currently loaded chunks.  Large
        # maps can have tens or hundreds of thousands of manifest cells; copying
        # every AABB into Python containers at map-open time defeats streaming's
        # memory cap before the first frame is drawn.
        self._chunk_aabbs = {}
        self._navigation_guard_cells = self.world.available_cells
        self._navigation_guard_chunk_size = chunk_size
        if self._navigation_guard_enabled:
            _LOG.info(
                "Navigation guard enabled: "
                f"{len(self._navigation_guard_cells)} occupied cells, "
                f"radius={self._navigation_guard_radius_cells} cell(s)."
            )

        # Render-distance slider's current value should drive the new
        # map's streaming config immediately, rather than resetting back
        # to the control's own default -- if someone already turned it up
        # for a previous large map, opening another large map shouldn't
        # silently reset that preference. (On first launch, from
        # __init__, this just re-applies the control's own initial value,
        # a harmless no-op.)
        if hasattr(self, "render_distance_stepper"):
            self.world.config.load_radius_cells = self.render_distance_stepper.value

        self.controls_overlay.show_fullscreen()
        # Reset on each map load; set True when the initial view has enough
        # uploaded chunks to be usable, not merely when the first chunk arrives.
        self._initial_chunks_loaded = False
        self._chunk_prep_progress = 0.0
        self._chunk_prep_complete_until = None
        self._chunk_prep_completion_armed = False
        self.import_progress_panel.reset_progress()

    def _navigation_position_is_allowed(self, position: np.ndarray) -> bool:
        """
        Keep free-fly navigation near occupied chunk cells, preventing users
        from drifting into empty map space where nothing will render.
        """
        if not self._navigation_guard_enabled:
            return True
        if not self._navigation_guard_cells or self._navigation_guard_chunk_size is None:
            return True

        cell = chunker.world_to_cell(np.asarray(position, dtype=np.float32), self._navigation_guard_chunk_size)
        radius = self._navigation_guard_radius_cells
        for dx in range(-radius, radius + 1):
            for dy in range(-radius, radius + 1):
                for dz in range(-radius, radius + 1):
                    if (cell[0] + dx, cell[1] + dy, cell[2] + dz) in self._navigation_guard_cells:
                        return True
        return False

    def _nearest_navigation_guard_position(self, position: np.ndarray) -> np.ndarray | None:
        """Return the nearest occupied chunk center for rare invalid camera positions."""
        if not self._navigation_guard_cells or self._navigation_guard_chunk_size is None:
            return None

        pos = np.asarray(position, dtype=np.float64)
        best_cell = None
        best_dist_sq = None
        chunk_size = float(self._navigation_guard_chunk_size)
        for cell in self._navigation_guard_cells:
            aabb = self._chunk_aabbs.get(cell)
            if aabb is not None:
                center = (aabb[0].astype(np.float64) + aabb[1].astype(np.float64)) * 0.5
            else:
                center = (np.array(cell, dtype=np.float64) + 0.5) * chunk_size
            dist_sq = float(np.sum((center - pos) ** 2))
            if best_dist_sq is None or dist_sq < best_dist_sq:
                best_dist_sq = dist_sq
                best_cell = cell

        if best_cell is None:
            return None

        aabb = self._chunk_aabbs.get(best_cell)
        if aabb is not None:
            return (aabb[0].astype(np.float64) + aabb[1].astype(np.float64)) * 0.5
        return (np.array(best_cell, dtype=np.float64) + 0.5) * chunk_size

    def _move_camera_guarded(self, forward_amt: float, right_amt: float, up_amt: float,
                             dt: float, speed_multiplier: float) -> None:
        if self.camera is None:
            return

        old_position = self.camera.position.copy()
        self.camera.move(forward_amt, right_amt, up_amt, dt, speed_multiplier)
        if self._navigation_position_is_allowed(self.camera.position):
            return

        self.camera.position = old_position

    def _recording_is_armed(self) -> bool:
        return self._ensure_recording_controller().is_armed(
            process_active=self._recording_session is not None
        )

    def _ensure_recording_stop_state(self) -> None:
        if not hasattr(self, "_recording_stop_results"):
            self._recording_stop_results = queue.Queue()
        if not hasattr(self, "_recording_stop_thread"):
            self._recording_stop_thread = None

    def _recording_stop_in_progress(self) -> bool:
        self._ensure_recording_stop_state()
        return self._recording_stop_thread is not None

    def _recording_hides_hud(self) -> bool:
        return self._recording_is_armed()

    def _toggle_recording(self) -> None:
        self._drain_recording_stop_results()
        if self._recording_stop_in_progress():
            self._show_recording_status(
                "Finishing recording",
                "Video is still being finalized.",
                kind="info",
                duration=2.0,
            )
            return

        if self._recording_session is not None:
            self._stop_recording(show_message=True)
            return

        if self._recording_countdown_until is not None:
            self._ensure_recording_controller().cancel_countdown(now=time.perf_counter())
            _LOG.info("Recording countdown canceled.")
            return

        self._start_recording_countdown()

    def _start_recording_countdown(self) -> None:
        if not self._has_map_loaded:
            return
        if self._resolve_ffmpeg_path() is None:
            self._recording_unavailable("ffmpeg was not found. Install dependencies or set CAVEVIEWER_FFMPEG.")
            return

        self.color_picker.hide()
        if self.controls_overlay.is_manual_mode:
            self.controls_overlay.hide_help()
        now = time.perf_counter()
        self._ensure_recording_controller().start_countdown(
            now=now,
            start_number=self.RECORDING_COUNTDOWN_START_NUMBER,
        )
        _LOG.info("Recording countdown started. Press Shift+R to cancel or stop.")

    def _resolve_ffmpeg_path(self) -> str | None:
        return recording.resolve_ffmpeg_path()

    def _recording_unavailable(self, reason: str) -> None:
        message = f"Cannot start recording: {reason}"
        _LOG.warning(message)
        self._show_recording_status(
            "Recording unavailable",
            reason,
            kind="error",
            duration=3.4,
        )

    def _recording_capture_viewport(self) -> tuple[int, int, int, int]:
        for viewport in (
            getattr(self.ctx, "viewport", None),
            getattr(self.ctx.screen, "viewport", None),
        ):
            if viewport and len(viewport) >= 4:
                x, y, width, height = (int(v) for v in viewport[:4])
                if width > 0 and height > 0:
                    return x, y, width, height

        screen_size = getattr(self.ctx.screen, "size", None)
        if screen_size:
            width, height = screen_size
            return 0, 0, int(width), int(height)

        width, height = self.wnd.size
        return 0, 0, int(width), int(height)

    def _recording_framebuffer_size(self) -> tuple[int, int]:
        _x, _y, width, height = self._recording_capture_viewport()
        return width, height

    def _recording_output_size(self, width: int, height: int) -> tuple[int, int]:
        return recording.recording_output_size(
            width,
            height,
            self._recording_max_height,
        )

    def _release_recording_readback_framebuffer(self) -> None:
        framebuffer = getattr(self, "_recording_readback_framebuffer", None)
        self._recording_readback_framebuffer = None
        if framebuffer is None:
            return
        try:
            framebuffer.release()
        except Exception:
            pass

    def _discard_recording_staged_frames(self) -> int:
        pending = getattr(self, "_recording_readback_pending", [])
        dropped = len(pending)
        for slot in pending:
            slot.in_flight = False
        pending.clear()
        return dropped

    def _release_recording_readback_buffers(self) -> None:
        self._discard_recording_staged_frames()
        slots = getattr(self, "_recording_readback_slots", [])
        self._recording_readback_slots = []
        self._recording_readback_pending = []
        self._recording_readback_byte_count = 0
        self._ensure_recording_controller().reset_frame_timings()
        for slot in slots:
            try:
                slot.buffer.release()
            except Exception:
                pass

    def _create_recording_readback_framebuffer(
        self,
        capture_size: tuple[int, int],
        output_size: tuple[int, int],
    ) -> moderngl.Framebuffer | None:
        self._release_recording_readback_framebuffer()
        if output_size == capture_size:
            return None

        # Downscale on the GPU before readback. Reading the full high-DPI
        # window framebuffer can block the render loop for tens of
        # milliseconds per recorded frame; reading the output-sized buffer
        # keeps the synchronized transfer much smaller.
        framebuffer = self.ctx.simple_framebuffer(output_size, components=4)
        framebuffer.viewport = (0, 0, output_size[0], output_size[1])
        self._recording_readback_framebuffer = framebuffer
        return framebuffer

    def _create_recording_readback_buffers(self, output_size: tuple[int, int]) -> None:
        self._release_recording_readback_buffers()
        width, height = output_size
        byte_count = width * height * self.RECORDING_READBACK_COMPONENTS
        slots = []
        try:
            for _ in range(self.RECORDING_READBACK_BUFFER_COUNT):
                slots.append(_RecordingReadbackSlot(self.ctx.buffer(reserve=byte_count)))
        except Exception:
            for slot in slots:
                try:
                    slot.buffer.release()
                except Exception:
                    pass
            raise
        self._recording_readback_slots = slots
        self._recording_readback_pending = []
        self._recording_readback_byte_count = byte_count

    def _start_recording_encoder(self) -> bool:
        ffmpeg_path = self._resolve_ffmpeg_path()
        if ffmpeg_path is None:
            self._recording_unavailable("ffmpeg was not found. Install dependencies or set CAVEVIEWER_FFMPEG.")
            self._ensure_recording_controller().clear_countdown()
            return False

        viewport = self._recording_capture_viewport()
        width, height = viewport[2], viewport[3]
        if width <= 0 or height <= 0:
            self._ensure_recording_controller().clear_countdown()
            return False

        try:
            os.makedirs(self._recording_output_dir, exist_ok=True)
        except OSError as exc:
            _LOG.warning(f"Cannot start recording: failed to create output directory: {exc}")
            self._ensure_recording_controller().clear_countdown()
            self._show_recording_status(
                "Recording unavailable",
                f"Cannot save to {self._recording_display_path(self._recording_output_dir)}",
                kind="error",
                duration=3.4,
            )
            return False

        timestamp = time.strftime("%Y%m%d-%H%M%S")
        output_path = os.path.join(self._recording_output_dir, f"CaveViewerDive-{timestamp}.mp4")
        output_width, output_height = self._recording_output_size(width, height)
        output_size = (output_width, output_height)
        try:
            readback_framebuffer = self._create_recording_readback_framebuffer(
                (width, height),
                output_size,
            )
            self._create_recording_readback_buffers(output_size)
        except Exception as exc:
            self._release_recording_readback_framebuffer()
            self._release_recording_readback_buffers()
            _LOG.warning(f"Cannot start recording: failed to create recording readback resources: {exc}")
            self._ensure_recording_controller().clear_countdown()
            self._show_recording_status(
                "Recording unavailable",
                "Could not prepare the recording framebuffer.",
                kind="error",
                duration=3.4,
            )
            return False

        try:
            session = recording.start_encoder_session(
                ffmpeg_path=ffmpeg_path,
                output_path=output_path,
                output_size=output_size,
                viewport=viewport,
                fps=self._recording_fps,
                crf=self._recording_crf,
                raw_pix_fmt=self.RECORDING_RAW_PIX_FMT,
                popen_startup_kwargs=(
                    self._active_platform_adapter().recording_subprocess_startup_kwargs()
                ),
            )
        except (OSError, RuntimeError) as exc:
            _LOG.warning(f"Cannot start recording: {exc}")
            self._release_recording_readback_framebuffer()
            self._release_recording_readback_buffers()
            self._ensure_recording_controller().clear_countdown()
            return False

        self._recording_session = session
        self._recording_frame_queue = session.frame_queue
        self._recording_output_path = session.output_path
        self._recording_size = session.output_size
        self._recording_viewport = session.viewport
        self._recording_readback_framebuffer = readback_framebuffer
        now = time.perf_counter()
        self._ensure_recording_controller().mark_encoder_started(now=now)
        _LOG.info(
            f"Recording started: {output_path} "
            f"capture_viewport={viewport} readback_size={output_width}x{output_height} "
            f"output_size={output_width}x{output_height} "
            f"raw_pix_fmt={self.RECORDING_RAW_PIX_FMT} "
            f"readback_buffers={len(self._recording_readback_slots)}"
        )
        return True

    def _recording_signal_writer_stop(self, frame_queue: queue.Queue | None) -> None:
        recording.signal_writer_stop(frame_queue)

    def _recording_drop_frames(self, count: int = 1) -> None:
        if self._ensure_recording_controller().drop_frames(count):
            _LOG.warning("Recording encoder is falling behind; dropping video frames.")

    def _recording_due_frame_slots(self, now: float, next_frame_time: float | None) -> int:
        return self._ensure_recording_controller().due_frame_slots(
            now=now,
            next_frame_time=next_frame_time,
        )

    def _recording_enqueue_frame(self, frame: bytes) -> bool:
        frame_queue = self._recording_frame_queue
        if frame_queue is None:
            self._stop_recording()
            return False

        try:
            frame_queue.put_nowait(frame)
        except queue.Full:
            self._recording_drop_frames()
            return False
        return True

    def _recording_display_path(self, path: str | None) -> str | None:
        return recording.recording_display_path(path)

    def _show_recording_status(
        self,
        message: str,
        detail: str | None = None,
        *,
        kind: str = "info",
        duration: float = 2.8,
    ) -> None:
        self._ensure_recording_controller().show_status(
            message,
            detail=detail,
            kind=kind,
            duration=duration,
            now=time.perf_counter(),
        )

    def _stop_recording(self, *, show_message: bool = False) -> None:
        self._ensure_recording_stop_state()
        self._drain_recording_stop_results()
        if self._recording_stop_in_progress():
            return

        session = self._recording_session
        output_path = session.output_path if session is not None else None

        self._ensure_recording_controller().clear_countdown()
        self._recording_session = None
        self._recording_output_path = None
        self._recording_size = None
        self._recording_viewport = None
        self._release_recording_readback_buffers()
        self._release_recording_readback_framebuffer()
        self._recording_next_frame_time = None
        self._recording_frame_queue = None

        if session is None:
            return

        session.signal_writer_stop()
        work = session.stop_work(show_message=show_message)
        self._recording_stop_thread = recording.start_stop_finalizer(
            work,
            result_queue=self._recording_stop_results,
            stderr_text=session.stderr_text,
            writer_error=lambda: session.writer_error,
            dropped_frames=lambda: self._recording_dropped_frames,
            logger=_LOG,
        )
        if show_message:
            self._show_recording_status(
                "Finishing recording",
                self._recording_display_path(output_path),
                kind="info",
                duration=2.0,
            )

    def _drain_recording_stop_results(self) -> None:
        self._ensure_recording_stop_state()
        while True:
            try:
                result = self._recording_stop_results.get_nowait()
            except queue.Empty:
                break
            self._apply_recording_stop_result(result)
            self._recording_stop_thread = None

    def _apply_recording_stop_result(self, result: _RecordingStopResult) -> None:
        self._ensure_recording_controller().reset_after_stop_result()

        if result.returncode == 0:
            _LOG.info(f"Recording saved: {result.output_path}")
            if result.dropped_frames:
                _LOG.warning(f"Recording saved after dropping {result.dropped_frames} frame(s).")
            if result.show_message:
                self._show_recording_status(
                    "Recording saved",
                    self._recording_display_path(result.output_path),
                    kind="success",
                    duration=3.2,
                )
        else:
            if result.stderr_text and result.writer_error:
                detail = f": {result.stderr_text}; writer_error={result.writer_error}"
            elif result.stderr_text:
                detail = f": {result.stderr_text}"
            elif result.writer_error:
                detail = f": writer_error={result.writer_error}"
            else:
                detail = ""
            _LOG.warning(f"Recording encoder exited with code {result.returncode}{detail}")
            if result.show_message:
                self._show_recording_status(
                    "Recording failed",
                    self._recording_failure_detail(result.stderr_text),
                    kind="error",
                    duration=3.4,
                )

    def _recording_failure_detail(self, stderr_text: str) -> str:
        return recording.recording_failure_detail(stderr_text)

    def _recording_capture_state(self) -> tuple[tuple[int, int], tuple[int, int, int, int], int]:
        if self._recording_size is None or self._recording_viewport is None:
            raise OSError("recording capture state is not initialized")
        byte_count = self._recording_readback_byte_count
        if byte_count <= 0:
            raise OSError("recording readback buffers are not initialized")
        return self._recording_size, self._recording_viewport, byte_count

    def _recording_free_readback_slot(self) -> _RecordingReadbackSlot | None:
        for slot in self._recording_readback_slots:
            if not slot.in_flight:
                return slot
        return None

    def _recording_copy_to_readback_framebuffer(
        self,
        readback_framebuffer: moderngl.Framebuffer,
        output_size: tuple[int, int],
        capture_viewport: tuple[int, int, int, int],
    ) -> None:
        screen = self.ctx.screen
        previous_screen_viewport = getattr(screen, "viewport", None)
        previous_readback_viewport = getattr(readback_framebuffer, "viewport", None)
        width, height = output_size
        try:
            screen.viewport = capture_viewport
            readback_framebuffer.viewport = (0, 0, width, height)
            self.ctx.copy_framebuffer(readback_framebuffer, screen)
        finally:
            if previous_screen_viewport is not None:
                try:
                    screen.viewport = previous_screen_viewport
                except Exception:
                    pass
            if previous_readback_viewport is not None:
                try:
                    readback_framebuffer.viewport = previous_readback_viewport
                except Exception:
                    pass

    def _recording_stage_frame(
        self,
        render_frame: Callable[[moderngl.Framebuffer, tuple[int, int]], None] | None = None,
    ) -> bool:
        output_size, capture_viewport, _byte_count = self._recording_capture_state()
        slot = self._recording_free_readback_slot()
        if slot is None:
            return False

        width, height = output_size
        readback_framebuffer = self._recording_readback_framebuffer
        if readback_framebuffer is None:
            self.ctx.screen.read_into(
                slot.buffer,
                viewport=capture_viewport,
                components=self.RECORDING_READBACK_COMPONENTS,
                alignment=1,
            )
        else:
            if render_frame is None:
                self._recording_copy_to_readback_framebuffer(
                    readback_framebuffer,
                    output_size,
                    capture_viewport,
                )
            else:
                render_frame(readback_framebuffer, output_size)
            readback_framebuffer.read_into(
                slot.buffer,
                viewport=(0, 0, width, height),
                components=self.RECORDING_READBACK_COMPONENTS,
                alignment=1,
            )

        slot.in_flight = True
        self._recording_readback_pending.append(slot)
        return True

    def _recording_drain_staged_frames(self) -> float:
        pending = self._recording_readback_pending
        slots = self._recording_readback_slots
        frame_queue = self._recording_frame_queue
        if (
            not pending
            or not slots
            or len(pending) < len(slots)
            or frame_queue is None
            or frame_queue.full()
        ):
            return 0.0

        _output_size, _capture_viewport, byte_count = self._recording_capture_state()
        slot = pending.pop(0)
        frame = None
        t_read = time.perf_counter()
        try:
            frame = slot.buffer.read(size=byte_count)
            read_ms = (time.perf_counter() - t_read) * 1000.0
            if len(frame) != byte_count:
                _LOG.warning(
                    "Recording stopped because framebuffer byte size changed: "
                    f"actual={len(frame)} expected={byte_count}."
                )
                self._stop_recording()
                return read_ms
            self._recording_enqueue_frame(frame)
            return read_ms
        finally:
            slot.in_flight = False
            frame = None

    def _recording_update_after_scene(
        self,
        now: float,
        *,
        render_frame: Callable[[moderngl.Framebuffer, tuple[int, int]], None] | None = None,
    ) -> float:
        controller = self._ensure_recording_controller()
        controller.reset_frame_timings()

        if controller.countdown_until is not None:
            if not controller.countdown_ready(now=now):
                return 0.0
            if not self._start_recording_encoder():
                return 0.0

        session = self._recording_session
        if session is None:
            return 0.0

        if session.stopped_before_finalization():
            _LOG.warning("Recording encoder stopped before recording was finalized.")
            self._stop_recording(show_message=True)
            return 0.0

        if self._recording_viewport != self._recording_capture_viewport():
            _LOG.warning("Recording stopped because the window size changed.")
            self._stop_recording()
            return 0.0

        read_ms = 0.0
        try:
            drain_ms = self._recording_drain_staged_frames()
            self._recording_last_drain_ms = drain_ms
            read_ms += drain_ms
        except (OSError, moderngl.Error) as exc:
            _LOG.warning(f"Recording stopped because frame capture failed: {exc}")
            self._stop_recording(show_message=True)
            return read_ms
        if self._recording_session is None:
            return read_ms

        next_frame_time = self._recording_next_frame_time
        if next_frame_time is not None and now < next_frame_time:
            return read_ms

        frame_slots = self._recording_due_frame_slots(now, next_frame_time)
        frame_queue = self._recording_frame_queue
        if frame_queue is None:
            self._stop_recording(show_message=True)
            return read_ms

        if frame_queue.full():
            staged_frames = self._discard_recording_staged_frames()
            self._recording_drop_frames(frame_slots + staged_frames)
            controller.advance_next_frame_time(now=now, frame_slots=frame_slots)
            return read_ms

        try:
            self._recording_drop_frames(frame_slots - 1)

            t_stage = time.perf_counter()
            if not self._recording_stage_frame(render_frame=render_frame):
                self._recording_drop_frames()
            stage_ms = (time.perf_counter() - t_stage) * 1000.0
            self._recording_last_stage_ms = stage_ms
            read_ms += stage_ms
            controller.advance_next_frame_time(now=now, frame_slots=frame_slots)
            return read_ms
        except (OSError, moderngl.Error) as exc:
            _LOG.warning(f"Recording stopped because frame capture failed: {exc}")
            self._stop_recording(show_message=True)
            return read_ms

    def _recording_countdown_display(self, now: float) -> tuple[int, float]:
        display = self._ensure_recording_controller().countdown_display(
            now=now,
            start_number=self.RECORDING_COUNTDOWN_START_NUMBER,
        )
        return display.number, display.progress

    def _print_texture_diagnostics(self, manifest: dict, textures_dir: str) -> None:
        """Print a one-time texture summary to console on map load."""
        from PIL import Image
        import io as _io

        mats = manifest.get("mtl_materials", {})
        _LOG.info(f"Texture diagnostics: {len(mats)} materials, "
              f"{len(manifest.get('chunks', {}))} total chunks")

        # Deduplicate: multiple material names can share one file/bytes blob.
        seen: dict[object, tuple[str, tuple[int, int]]] = {}  # key -> (first_mat, size)
        missing = 0
        embedded = 0

        for mat_name, file_or_bytes in mats.items():
            if file_or_bytes is None:
                missing += 1
                continue
            if file_or_bytes in seen:
                continue
            if isinstance(file_or_bytes, bytes):
                embedded += 1
                try:
                    img = Image.open(_io.BytesIO(file_or_bytes))
                    seen[file_or_bytes] = (mat_name, img.size)
                except Exception:
                    seen[file_or_bytes] = (mat_name, (0, 0))
            else:
                import os as _os
                path = _os.path.join(textures_dir, file_or_bytes)
                try:
                    with Image.open(path) as img:
                        seen[file_or_bytes] = (mat_name, img.size)
                except Exception:
                    seen[file_or_bytes] = (mat_name, (0, 0))

        sizes = [sz for _, sz in seen.values() if sz != (0, 0)]
        unique_files = len(seen)
        total_px = sum(w * h for w, h in sizes)
        total_mb = total_px * 3 / (1024 * 1024)  # RGB uncompressed

        size_counts: dict[tuple, int] = {}
        for sz in sizes:
            size_counts[sz] = size_counts.get(sz, 0) + 1

        _LOG.info(f"  Unique texture files : {unique_files}"
                  + (f" ({embedded} embedded)" if embedded else ""))
        if missing:
            _LOG.info(f"  Materials with no texture: {missing}")
        for sz, count in sorted(size_counts.items(), key=lambda x: -x[1]):
            _LOG.info(f"  {sz[0]}x{sz[1]} : {count} texture(s)")
        _LOG.info(f"  Uncompressed RGB total  : {total_mb:.0f} MB")
        max_dim = max((max(w, h) for w, h in sizes), default=0)
        # Rough atlas fit: next power-of-2 square that holds total_px
        import math as _math
        atlas_side = 2 ** _math.ceil(_math.log2(_math.sqrt(total_px))) if total_px > 0 else 0
        _LOG.info(f"  Estimated atlas needed  : {atlas_side}x{atlas_side} px "
              f"({atlas_side*atlas_side*3/1024/1024:.0f} MB)")

    def _teardown_current_map(self, *, final_shutdown: bool = False) -> None:
        """
        Cleanly releases everything specific to the CURRENTLY loaded map
        before _load_map() builds a new one -- stops StreamingWorld's
        background threads, then
        releases every currently-resident chunk's GPU buffers/VAOs and
        decrements the texture manager's reference counts via the exact
        same _on_chunk_unload() path used during normal streaming (so
        there's no separate cleanup logic to keep in sync with the
        regular unload path). The texture manager itself is then simply
        discarded -- a fresh one is constructed for the new map rather
        than trying to partially reuse the old one.

        Safe to call even if no map was ever loaded yet (e.g. the very
        first import, triggered from _run_pending_import, completing for
        the first time rather than switching away from an existing map)
        -- there's nothing to tear down in that case, so this just
        returns immediately rather than crashing on self.world not
        existing yet.

        Shutdown uses a finite worker-join timeout even during final window
        close.  Streaming workers are CPU/I/O-only and never issue OpenGL
        commands; if one is stuck in external I/O, StreamingWorld records and
        logs the unjoined worker instead of letting the viewer close callback
        block forever.
        """
        if not self._has_map_loaded:
            return

        self._stop_recording()
        # Keep this callback bounded: on_close() runs inside the window/render
        # event path, and an unbounded join here can leave the viewer visually
        # frozen if a streaming worker is stuck in disk or callback code.
        self.world.shutdown(timeout=_VIEWER_STREAMING_SHUTDOWN_TIMEOUT_SECONDS)

        for cell in list(getattr(self, "_chunk_upload_states", {}).keys()):
            self._on_chunk_unload(cell)

        for cell in list(self._chunk_gpu_objects.keys()):
            self._on_chunk_unload(cell)

        # belt-and-suspenders: if anything was somehow left behind (it
        # shouldn't be, given the loop above), don't carry it into the
        # next map's state
        self._chunk_gpu_objects.clear()
        self._chunk_upload_states.clear()
        self._chunk_normal_cache.clear()
        self._chunk_aabbs.clear()
        self._navigation_guard_cells = set()
        self._navigation_guard_chunk_size = None

        if hasattr(self, "texture_manager") and self.texture_manager is not None:
            self.texture_manager.shutdown()

        if self.minimap is not None:
            try:
                self.minimap.release()
            except Exception:
                pass

        self._has_map_loaded = False
        self.world = None
        self.camera = None
        self.minimap = None
        self.texture_manager = None

    def _release_window_resources(self) -> None:
        """Release non-map GPU/UI resources when closing the viewer window."""
        if self._window_resources_released:
            return
        self._window_resources_released = True

        self._stop_recording()
        self._keys_down.clear()
        self._mouse_look_active = False
        self._mouse_look_left_option_active = False
        self._last_mouse_pos = None

        # on_close() asks the import controller to stop any active import before
        # resource teardown. Drop remaining refs here so detached fallback
        # messages cannot be applied after the window closes.
        self._import_active = False
        self._import_queue = None
        self._import_thread = None
        self._import_command_queue = None

        def _release_attr(obj, attr_name: str) -> None:
            resource = getattr(obj, attr_name, None)
            if resource is None:
                return
            if hasattr(resource, "release"):
                try:
                    resource.release()
                except Exception:
                    pass
            try:
                setattr(obj, attr_name, None)
            except Exception:
                pass

        components = (
            "light_stepper",
            "render_distance_stepper",
            "ambient_stepper",
            "render_mode_buttons",
            "controls_overlay",
            "color_picker",
            "import_progress_panel",
            "minimap",
        )
        for name in components:
            obj = getattr(self, name, None)
            if obj is None:
                continue
            _release_attr(obj, "_vao")
            _release_attr(obj, "_vbo")
            _release_attr(obj, "program")
            if hasattr(obj, "release"):
                try:
                    obj.release()
                except Exception:
                    pass
            setattr(self, name, None)

        _release_attr(self, "program")
        _release_attr(self, "_hud_panel_vao")
        _release_attr(self, "_hud_panel_vbo")
        _release_attr(self, "_status_panel_vao")
        _release_attr(self, "_status_panel_vbo")
        _release_attr(self, "_hud_panel_program")

        CaveViewerWindow.cave_cache_dir = None
        CaveViewerWindow.cave_textures_dir = None
        CaveViewerWindow.cave_manifest = None
        CaveViewerWindow.cave_pending_import = None

    def load_new_map(
        self,
        cache_dir: str,
        textures_dir: str,
        manifest: dict,
        *,
        source_dir: str | None = None,
    ) -> None:
        """
        Switches the viewer to a different map without closing the
        window -- called by the OPEN button's click handler once a new
        folder has been picked and imported/cached (see
        caveviewer.app's find_input_files/import_and_cache, reused as-is
        rather than duplicated here).

        Order matters: tear down the OLD map's GPU/thread state fully
        before constructing any NEW state, rather than interleaving the
        two -- this guarantees the old map's resources are genuinely
        released (not just about to be overwritten by Python references
        moving on, which would leak the GPU-side buffers/textures since
        those aren't cleaned up by garbage collection alone).
        """
        self._teardown_current_map()
        self._load_map(cache_dir, textures_dir, manifest)
        self._has_map_loaded = True
        try:
            from caveviewer.gui.map_history import remember_recent_map_path

            remember_recent_map_path(source_dir or textures_dir)
        except Exception:
            pass

    def _handle_open_button_click(self) -> None:
        """
        Full OPEN button flow: shows the folder-browse dialog (same one
        used at startup), detects which supported format (.obj or
        .glb) the selected folder contains, imports/caches it if there's
        no valid cache yet (showing the progress panel while that one-
        time work runs), and finally calls load_new_map() to actually
        switch.

        Any failure along the way (cancelled dialog, no supported model
        file found, import error) prints a clear message and leaves the
        CURRENTLY loaded map running untouched -- a failed attempt to
        open a different map should never take down the map you already
        had open and were presumably still looking at.
        """
        folder = pick_folder_dialog()
        if not folder:
            _LOG.info("Open cancelled -- no folder selected.")
            return

        _LOG.info(f"Opening new map from: {os.path.abspath(folder)}")

        try:
            open_target = resolve_selected_map_folder(folder)
        except FileNotFoundError as e:
            _LOG.warning(f"Could not open this folder: {e}")
            return
        except Exception as manifest_err:
            _LOG.error(f"Failed to load the selected prebuilt map: {manifest_err}")
            return

        if open_target.is_prebuilt_cache:
            _LOG.info(f"Found cache manifest in selected directory: {open_target.cache_dir}")
            _LOG.info(f"Switching to prebuilt map: {open_target.map_name}")
            _LOG.info(f"Using cache directory: {open_target.cache_dir}")
            self.load_new_map(
                open_target.cache_dir,
                open_target.textures_dir,
                open_target.manifest,
                source_dir=open_target.source_dir,
            )
            _LOG.info(f"Now viewing: {open_target.map_name}")
            return

        self._start_import_async(
            open_target.model_descriptor,
            open_target.textures_dir,
            open_target.map_name,
            is_startup=False,
        )

    def _import_model_format_from_descriptor(self, model_descriptor: dict) -> str | None:
        return self._ensure_import_controller().import_model_format_from_descriptor(
            model_descriptor
        )

    def _default_import_progress_note(self) -> str:
        return self._ensure_import_controller().default_progress_note()

    def _set_import_progress_message(self, title: str, note: str) -> None:
        self._ensure_import_controller().set_progress_message(title, note)

    def _update_import_progress_message_for_stage(self, stage: str) -> None:
        self._ensure_import_controller().update_progress_message_for_stage(stage)

    def _show_import_pause_notice(
        self,
        map_name: str,
        *,
        close_after: bool = False,
        duration: float = 6.0,
    ) -> None:
        self._ensure_import_controller().show_pause_notice(
            map_name,
            close_after=close_after,
            duration=duration,
        )

    def _clear_import_pause_notice(self) -> bool:
        return self._ensure_import_controller().clear_pause_notice()

    def _render_import_pause_notice_if_active(self) -> bool:
        return self._ensure_import_controller().render_pause_notice_if_active(
            self.import_progress_panel,
            self.wnd,
        )

    def _render_pending_import_splash(self) -> None:
        self._ensure_import_controller().render_pending_import_splash(
            CaveViewerWindow.cave_pending_import,
            self.import_progress_panel,
            self.wnd.size,
        )

    def _present_pending_import_splash_now(self) -> bool:
        """Best-effort immediate splash presentation during window setup."""
        try:
            self._render_pending_import_splash()
        except Exception as exc:
            _LOG.debug("Could not render early import splash: %s", exc)
            return False

        for target in (
            getattr(self, "wnd", None),
            getattr(getattr(self, "wnd", None), "_window", None),
        ):
            if target is None:
                continue
            for method_name in ("swap_buffers", "flip", "swap"):
                swap = getattr(target, method_name, None)
                if not callable(swap):
                    continue
                try:
                    swap()
                    return True
                except Exception as exc:
                    _LOG.debug(
                        "Could not present early import splash with %s.%s: %s",
                        type(target).__name__,
                        method_name,
                        exc,
                    )
        return False

    def _start_import_async(
        self,
        model_descriptor: dict,
        textures_dir: str,
        map_name: str,
        is_startup: bool = False,
    ) -> None:
        self._ensure_import_controller().start_async(
            model_descriptor,
            textures_dir,
            map_name,
            is_startup=is_startup,
        )

    def _drain_import_queue(self) -> None:
        self._ensure_import_controller().drain_queue()

    def _run_pending_import(self) -> None:
        """
        Runs the FIRST-TIME import for the map the program was launched
        with, when CaveViewerWindow.cave_pending_import was set instead
        of an already-built cache (see run_viewer_with_pending_import()
        at the bottom of this file, and main()'s use of it in
        caveviewer.app). Called once, from on_render()'s first frame --
        see the _has_map_loaded branch there for why it's deferred to
        that point rather than running before the window even opens.

        Format-agnostic: works the same regardless of whether the
        pending import is an .obj or .glb (see
        caveviewer.app's find_model_file()/import_and_cache_any(), which
        this delegates the actual format-specific parsing to) -- this
        method only deals with the progress-panel/window-lifecycle side
        of things, not anything about the source format itself.

        Shares the exact same import-with-progress-panel approach as
        _handle_open_button_click() (the OPEN button's mid-session
        equivalent of this), just sourced from the pending-import details
        already resolved by main() rather than a fresh folder-browse
        dialog + find_model_file() call.

        Unlike the OPEN button's failure handling (which can safely leave
        a previously-loaded map running untouched), a failure HERE means
        there was never a map to fall back to at all -- so this prints a
        clear error and closes the window instead, rather than leaving
        the person staring at a permanently blank screen with no map and
        no way to get one without restarting the program anyway.
        """
        pending = CaveViewerWindow.cave_pending_import
        model_descriptor = pending["model_descriptor"]
        textures_dir = pending["textures_dir"]
        source_path = model_descriptor.get("obj_path") or model_descriptor.get("glb_path")
        map_name = os.path.basename(source_path)
        self._start_import_async(model_descriptor, textures_dir, map_name, is_startup=True)

    # -- chunk GPU lifecycle ------------------------------------------------

    @staticmethod
    def _new_streaming_frame_timing() -> dict:
        return render_upload.new_streaming_frame_timing()

    @staticmethod
    def _format_optional_ms(value: float | None) -> str:
        return render_upload.format_optional_ms(value)

    @staticmethod
    def _format_streaming_frame_timing(timing: dict) -> str:
        return render_upload.format_streaming_frame_timing(timing)

    @staticmethod
    def _new_chunk_upload_counters() -> dict:
        return render_upload.new_chunk_upload_counters()

    @staticmethod
    def _add_chunk_upload_counters(target: dict, source: dict) -> None:
        render_upload.add_chunk_upload_counters(target, source)

    @staticmethod
    def _add_texture_timing_counters(
        counters: dict,
        texture_timing: dict,
        frame_timing: dict | None,
    ) -> None:
        render_upload.add_texture_timing_counters(
            counters,
            texture_timing,
            frame_timing,
        )

    def _render_upload_slice_vertices(self) -> int:
        return render_upload.render_upload_slice_vertices(
            getattr(
                self,
                "_vbo_upload_slice_bytes",
                _RENDER_UPLOAD_INITIAL_SLICE_BYTES,
            )
        )

    @staticmethod
    def _min_vbo_upload_slice_bytes() -> int:
        return render_upload.min_vbo_upload_slice_bytes()

    def _record_upload_slice_sizes(self, timing: dict | None) -> None:
        render_upload.record_upload_slice_sizes(
            timing,
            render_upload.UploadSliceState(
                vbo_upload_slice_bytes=int(
                    getattr(
                        self,
                        "_vbo_upload_slice_bytes",
                        _RENDER_UPLOAD_INITIAL_SLICE_BYTES,
                    )
                ),
                texture_upload_slice_bytes=int(
                    getattr(
                        self,
                        "_texture_upload_slice_bytes",
                        _RENDER_UPLOAD_INITIAL_SLICE_BYTES,
                    )
                ),
            ),
        )

    def _adapt_upload_slice_size(
        self,
        *,
        kind: str,
        elapsed_ms: float,
        byte_count: int,
        timing: dict | None,
    ) -> None:
        """
        Shrink future render-thread upload slices after a measured stall.

        The OpenGL driver can block on a single texture or buffer write even
        when the payload is already small. This feedback path keeps normal
        settings automatic: if one operation exceeds the frame upload target,
        later operations use a smaller byte budget instead of asking the user
        to keep tuning environment variables.
        """
        target_ms = float(
            getattr(
                self,
                "_current_upload_time_budget_ms",
                getattr(
                    self,
                    "_upload_time_budget_ms",
                    3.0,
                ),
            )
        )
        decision = render_upload.adapt_upload_slice_size(
            kind=kind,
            elapsed_ms=elapsed_ms,
            byte_count=byte_count,
            target_ms=target_ms,
            state=render_upload.UploadSliceState(
                vbo_upload_slice_bytes=int(
                    getattr(
                        self,
                        "_vbo_upload_slice_bytes",
                        _RENDER_UPLOAD_INITIAL_SLICE_BYTES,
                    )
                ),
                texture_upload_slice_bytes=int(
                    getattr(
                        self,
                        "_texture_upload_slice_bytes",
                        _RENDER_UPLOAD_INITIAL_SLICE_BYTES,
                    )
                ),
            ),
            timing=timing,
        )
        self._vbo_upload_slice_bytes = decision.state.vbo_upload_slice_bytes
        self._texture_upload_slice_bytes = decision.state.texture_upload_slice_bytes

    @staticmethod
    def _new_chunk_group_upload_job(group, smooth_shading: bool) -> dict:
        return render_upload.new_chunk_group_upload_job(group, smooth_shading)

    def _cancel_chunk_group_upload_job(self, job: dict | None) -> None:
        if not job:
            return
        texture_task = job.get("texture_task")
        cancel_acquire_task = getattr(self.texture_manager, "cancel_acquire_task", None)
        if texture_task is not None and callable(cancel_acquire_task):
            cancel_acquire_task(texture_task)
        if job.get("texture") is not None:
            self.texture_manager.release(job["group"].material_name)
            job["texture"] = None
        pending_vbo = job.get("pending_vbo")
        if pending_vbo is not None and hasattr(pending_vbo, "release"):
            pending_vbo.release()
        job["pending_vbo"] = None
        job["pending_vbo_payload"] = None

    def _advance_texture_upload_job(
        self,
        job: dict,
        counters: dict,
        timing: dict | None,
    ) -> bool:
        """Return True when the current render-upload job has a texture."""
        if job.get("texture") is not None:
            return True

        texture_task = job.get("texture_task")
        begin_acquire = getattr(self.texture_manager, "begin_acquire_with_timing", None)
        advance_acquire = getattr(
            self.texture_manager,
            "advance_acquire_with_timing",
            None,
        )
        if texture_task is None and callable(begin_acquire) and callable(advance_acquire):
            t_texture_begin = time.perf_counter()
            texture_task = begin_acquire(job["group"].material_name)
            counters["texture_ms"] += (
                time.perf_counter() - t_texture_begin
            ) * 1000.0
            job["texture_task"] = texture_task
            if texture_task.complete:
                job["texture"] = texture_task.result_texture
                job["texture_task"] = None
                self._add_texture_timing_counters(counters, texture_task.timing, timing)
                return True

        if texture_task is not None and callable(advance_acquire):
            texture, texture_timing, complete = advance_acquire(
                texture_task,
                max_upload_bytes=max(
                    1,
                    int(
                        getattr(
                            self,
                            "_texture_upload_slice_bytes",
                            _RENDER_UPLOAD_SLICE_BYTES,
                        )
                    ),
                ),
            )
            counters["texture_ms"] += texture_timing.get("total_ms", 0.0)
            self._add_texture_timing_counters(counters, texture_timing, timing)
            self._adapt_upload_slice_size(
                kind="texture",
                elapsed_ms=texture_timing.get("total_ms", 0.0),
                byte_count=int(texture_timing.get("image_bytes", 0)),
                timing=timing,
            )
            if complete:
                job["texture"] = texture
                job["texture_task"] = None
                return True
            return False

        t_texture = time.perf_counter()
        acquire_with_timing = getattr(
            self.texture_manager,
            "acquire_with_timing",
            None,
        )
        texture_timing = None
        if callable(acquire_with_timing):
            texture, texture_timing = acquire_with_timing(job["group"].material_name)
        else:
            texture = self.texture_manager.acquire(job["group"].material_name)
        counters["texture_ms"] += (time.perf_counter() - t_texture) * 1000.0
        if texture_timing is not None:
            self._add_texture_timing_counters(counters, texture_timing, timing)
            self._adapt_upload_slice_size(
                kind="texture",
                elapsed_ms=texture_timing.get("total_ms", texture_timing.get("texture_ms", 0.0)),
                byte_count=int(texture_timing.get("image_bytes", 0)),
                timing=timing,
            )
        job["texture"] = texture
        return True

    def _append_chunk_vbo_slice(
        self,
        job: dict,
        chunk_state: dict,
        vbo,
        start_vertex: int,
        end_vertex: int,
        counters: dict,
    ) -> bool:
        group = job["group"]
        t_vao = time.perf_counter()
        vao = self.ctx.vertex_array(
            self.program, [(vbo, "3f 2f 3f", "in_position", "in_uv", "in_normal")]
        )
        counters["vao_ms"] += (time.perf_counter() - t_vao) * 1000.0

        chunk_state["vao_list"].append(
            (vao, vbo, group.material_name, job["texture"])
        )
        chunk_state["normal_cache_entry"].append(
            (
                group.material_name,
                group.positions[start_vertex:end_vertex],
                group.uvs[start_vertex:end_vertex],
                group.smooth_normals[start_vertex:end_vertex],
            )
        )
        job["texture"] = None
        job["next_vertex_index"] = end_vertex
        if end_vertex >= len(group.positions):
            counters["groups"] += 1
            return True
        return False

    def _complete_pending_vbo_upload_job(
        self,
        job: dict,
        chunk_state: dict,
        counters: dict,
        timing: dict | None,
    ) -> bool:
        vbo = job["pending_vbo"]
        payload = job["pending_vbo_payload"]
        start_vertex = int(job["pending_vbo_start_vertex"])
        end_vertex = int(job["pending_vbo_end_vertex"])
        byte_count = len(payload)

        t_buffer = time.perf_counter()
        vbo.write(payload)
        elapsed_ms = (time.perf_counter() - t_buffer) * 1000.0
        counters["buffer_ms"] += elapsed_ms
        counters["buffer_write_ms"] += elapsed_ms
        counters["buffer_write_bytes"] += byte_count
        counters["vertices"] += end_vertex - start_vertex
        counters["bytes"] += byte_count
        self._adapt_upload_slice_size(
            kind="vbo",
            elapsed_ms=elapsed_ms,
            byte_count=byte_count,
            timing=timing,
        )

        complete = self._append_chunk_vbo_slice(
            job,
            chunk_state,
            vbo,
            start_vertex,
            end_vertex,
            counters,
        )
        job["pending_vbo"] = None
        job["pending_vbo_payload"] = None
        job["pending_vbo_start_vertex"] = 0
        job["pending_vbo_end_vertex"] = 0
        return complete

    def _advance_chunk_group_upload_job(
        self,
        job: dict,
        chunk_state: dict,
        counters: dict,
        timing: dict | None,
    ) -> bool:
        """
        Advance one render-thread upload operation for a material group.

        A group is deliberately split into a resumable texture acquire and
        triangle-aligned VBO slices. This keeps the streaming frame budget from
        starting a single large ``ctx.texture`` or ``ctx.buffer`` call that can
        monopolize the render thread.
        """
        if not self._advance_texture_upload_job(job, counters, timing):
            return False

        if job.get("pending_vbo") is not None:
            return self._complete_pending_vbo_upload_job(
                job,
                chunk_state,
                counters,
                timing,
            )

        group = job["group"]
        start_vertex = int(job["next_vertex_index"])
        vertex_count = len(group.positions)
        if start_vertex >= vertex_count:
            return True

        end_vertex = min(
            vertex_count,
            start_vertex + self._render_upload_slice_vertices(),
        )
        if end_vertex < vertex_count:
            end_vertex -= (end_vertex - start_vertex) % 3
            if end_vertex <= start_vertex:
                end_vertex = min(vertex_count, start_vertex + 3)

        used_prepacked = group.has_prepacked_vertex_bytes(
            smooth_shading=job["smooth_shading"]
        )
        t_pack = time.perf_counter()
        if used_prepacked:
            byte_start = start_vertex * _RENDER_UPLOAD_VERTEX_BYTES
            byte_end = end_vertex * _RENDER_UPLOAD_VERTEX_BYTES
            active_bytes = memoryview(group.prepacked_vertex_bytes)[
                byte_start:byte_end
            ]
        else:
            active_bytes = chunker.vertex_bytes_for_shading(
                group.positions[start_vertex:end_vertex],
                group.uvs[start_vertex:end_vertex],
                group.smooth_normals[start_vertex:end_vertex],
                smooth_shading=job["smooth_shading"],
            )
        counters["vertex_pack_ms"] += (time.perf_counter() - t_pack) * 1000.0
        if used_prepacked:
            counters["prepacked_groups"] += 1
        else:
            counters["fallback_pack_groups"] += 1
        byte_count = len(active_bytes)
        counters["buffer_alloc_bytes"] += byte_count

        t_buffer = time.perf_counter()
        try:
            vbo = self.ctx.buffer(reserve=byte_count)
        except TypeError:
            t_buffer = time.perf_counter()
            vbo = self.ctx.buffer(active_bytes)
            elapsed_ms = (time.perf_counter() - t_buffer) * 1000.0
            counters["buffer_ms"] += elapsed_ms
            counters["buffer_alloc_ms"] += elapsed_ms
            counters["buffer_write_ms"] += elapsed_ms
            counters["buffer_write_bytes"] += byte_count
            counters["vertices"] += end_vertex - start_vertex
            counters["bytes"] += byte_count
            self._adapt_upload_slice_size(
                kind="vbo",
                elapsed_ms=elapsed_ms,
                byte_count=byte_count,
                timing=timing,
            )
            try:
                return self._append_chunk_vbo_slice(
                    job,
                    chunk_state,
                    vbo,
                    start_vertex,
                    end_vertex,
                    counters,
                )
            except Exception:
                if hasattr(vbo, "release"):
                    vbo.release()
                raise

        elapsed_ms = (time.perf_counter() - t_buffer) * 1000.0
        counters["buffer_ms"] += elapsed_ms
        counters["buffer_alloc_ms"] += elapsed_ms
        self._adapt_upload_slice_size(
            kind="vbo",
            elapsed_ms=elapsed_ms,
            byte_count=byte_count,
            timing=timing,
        )
        job["pending_vbo"] = vbo
        job["pending_vbo_payload"] = active_bytes
        job["pending_vbo_start_vertex"] = start_vertex
        job["pending_vbo_end_vertex"] = end_vertex
        return False

    def _record_chunk_upload_timing(
        self,
        timing: dict | None,
        counters: dict,
        *,
        chunk_ms: float,
        cell,
        completed: bool,
    ) -> None:
        if timing is None:
            return

        timing["chunk_ready_ms"] += chunk_ms
        timing["chunk_prepare_ms"] += counters["chunk_prepare_ms"]
        timing["vertex_pack_ms"] += counters["vertex_pack_ms"]
        timing["buffer_ms"] += counters["buffer_ms"]
        timing["buffer_alloc_ms"] += counters["buffer_alloc_ms"]
        timing["buffer_write_ms"] += counters["buffer_write_ms"]
        timing["buffer_alloc_bytes"] += counters["buffer_alloc_bytes"]
        timing["buffer_write_bytes"] += counters["buffer_write_bytes"]
        timing["vao_ms"] += counters["vao_ms"]
        timing["texture_ms"] += counters["texture_ms"]
        timing["texture_decode_ms"] += counters["texture_decode_ms"]
        timing["texture_alloc_ms"] += counters["texture_alloc_ms"]
        timing["texture_write_ms"] += counters["texture_write_ms"]
        timing["texture_upload_ms"] += counters["texture_upload_ms"]
        timing["texture_mipmap_ms"] += counters["texture_mipmap_ms"]
        timing["texture_image_bytes"] += counters["texture_image_bytes"]
        timing["texture_material_cache_hits"] += counters["texture_material_cache_hits"]
        timing["texture_file_cache_hits"] += counters["texture_file_cache_hits"]
        timing["texture_decoded_cache_hits"] += counters["texture_decoded_cache_hits"]
        timing["texture_sync_decodes"] += counters["texture_sync_decodes"]
        timing["texture_placeholders"] += counters["texture_placeholders"]
        timing["chunk_bookkeeping_ms"] += counters["chunk_bookkeeping_ms"]
        if completed:
            timing["chunks_uploaded"] += 1
        timing["groups_uploaded"] += counters["groups"]
        timing["prepacked_groups"] += counters["prepacked_groups"]
        timing["fallback_pack_groups"] += counters["fallback_pack_groups"]
        timing["vertices_uploaded"] += counters["vertices"]
        timing["bytes_uploaded"] += counters["bytes"]
        if chunk_ms > timing["worst_chunk_ms"]:
            timing["worst_chunk_ms"] = chunk_ms
            timing["worst_chunk_cell"] = cell
            timing["worst_chunk_groups"] = counters["groups"]
            timing["worst_chunk_vertices"] = counters["vertices"]
            timing["worst_chunk_bytes"] = counters["bytes"]
            timing["worst_chunk_prepare_ms"] = counters["chunk_prepare_ms"]
            timing["worst_chunk_vertex_pack_ms"] = counters["vertex_pack_ms"]
            timing["worst_chunk_buffer_ms"] = counters["buffer_ms"]
            timing["worst_chunk_buffer_alloc_ms"] = counters["buffer_alloc_ms"]
            timing["worst_chunk_buffer_write_ms"] = counters["buffer_write_ms"]
            timing["worst_chunk_vao_ms"] = counters["vao_ms"]
            timing["worst_chunk_texture_ms"] = counters["texture_ms"]
            timing["worst_chunk_bookkeeping_ms"] = counters["chunk_bookkeeping_ms"]

    def _publish_chunk_upload_state(self, chunk_data, state: dict) -> None:
        """Make completed upload slices drawable before the whole chunk is done."""
        if not state.get("vao_list"):
            return
        self._chunk_gpu_objects[chunk_data.cell] = state["vao_list"]
        self._chunk_normal_cache[chunk_data.cell] = state["normal_cache_entry"]
        self._chunk_aabbs[chunk_data.cell] = (
            chunk_data.bounds_min.astype(np.float32, copy=False),
            chunk_data.bounds_max.astype(np.float32, copy=False),
        )

    def _on_chunk_ready(self, chunk_data):
        timing = getattr(self, "_streaming_frame_timing", None)
        chunk_start = time.perf_counter()
        frame_counters = self._new_chunk_upload_counters()

        upload_states = getattr(self, "_chunk_upload_states", None)
        if upload_states is None:
            self._chunk_upload_states = {}
            upload_states = self._chunk_upload_states

        state = upload_states.get(chunk_data.cell)
        if state is None:
            upload_groups = chunk_data.upload_groups
            if upload_groups is None:
                t_prepare = time.perf_counter()
                chunker.prepare_chunk_upload_groups(chunk_data)
                frame_counters["chunk_prepare_ms"] = (
                    time.perf_counter() - t_prepare
                ) * 1000.0
                upload_groups = chunk_data.upload_groups or []

            state = {
                "upload_groups": upload_groups or [],
                "next_group_index": 0,
                "active_group_job": None,
                "vao_list": [],
                "normal_cache_entry": [],
                "smooth_shading": bool(
                    self.render_mode_buttons.smooth_shading_enabled
                ),
            }
            upload_states[chunk_data.cell] = state

        max_groups = max(
            1,
            int(
                getattr(
                    self,
                    "_current_upload_operations_per_chunk",
                    getattr(self, "_upload_groups_per_frame", 1),
                )
            ),
        )
        time_budget_ms = max(
            0.5,
            float(
                getattr(
                    self,
                    "_current_upload_time_budget_ms",
                    getattr(self, "_upload_time_budget_ms", 3.0),
                )
            ),
        )
        operations_this_call = 0
        upload_groups = state["upload_groups"]

        while state["next_group_index"] < len(upload_groups):
            if operations_this_call >= max_groups:
                break
            if (
                operations_this_call > 0
                and (time.perf_counter() - chunk_start) * 1000.0 >= time_budget_ms
            ):
                break

            group_job = state.get("active_group_job")
            if group_job is None:
                group_job = self._new_chunk_group_upload_job(
                    upload_groups[state["next_group_index"]],
                    state["smooth_shading"],
                )
                state["active_group_job"] = group_job

            group_counters = self._new_chunk_upload_counters()
            group_complete = self._advance_chunk_group_upload_job(
                group_job,
                state,
                group_counters,
                timing,
            )
            operations_this_call += 1
            self._add_chunk_upload_counters(frame_counters, group_counters)
            self._publish_chunk_upload_state(chunk_data, state)
            if group_complete:
                state["active_group_job"] = None
                state["next_group_index"] += 1

        completed = state["next_group_index"] >= len(upload_groups)
        if completed:
            t_book = time.perf_counter()
            self._publish_chunk_upload_state(chunk_data, state)
            frame_counters["chunk_bookkeeping_ms"] += (
                time.perf_counter() - t_book
            ) * 1000.0
            del upload_states[chunk_data.cell]
            if state["smooth_shading"] != bool(
                self.render_mode_buttons.smooth_shading_enabled
            ):
                self._apply_shading_toggle_to_cell(chunk_data.cell)

        chunk_ms = (time.perf_counter() - chunk_start) * 1000.0
        self._record_chunk_upload_timing(
            timing,
            frame_counters,
            chunk_ms=chunk_ms,
            cell=chunk_data.cell,
            completed=completed,
        )
        return completed

    def _on_chunk_unload(self, cell):
        t_unload = time.perf_counter()
        partial_state = getattr(self, "_chunk_upload_states", {}).pop(cell, None)
        partial_was_published = (
            partial_state is not None
            and self._chunk_gpu_objects.get(cell)
            is partial_state.get("vao_list")
        )
        if partial_state is not None:
            self._cancel_chunk_group_upload_job(
                partial_state.get("active_group_job")
            )
            for vao, vbo, mat_name, texture in partial_state.get("vao_list", []):
                vao.release()
                vbo.release()
                self.texture_manager.release(mat_name)
        if partial_was_published:
            self._chunk_gpu_objects.pop(cell, None)
        else:
            vao_list = self._chunk_gpu_objects.pop(cell, [])
            for vao, vbo, mat_name, texture in vao_list:
                vao.release()
                vbo.release()
                self.texture_manager.release(mat_name)
        self._chunk_normal_cache.pop(cell, None)
        self._chunk_aabbs.pop(cell, None)
        timing = getattr(self, "_streaming_frame_timing", None)
        if timing is not None:
            timing["unload_ms"] += (time.perf_counter() - t_unload) * 1000.0
            timing["chunks_unloaded"] += 1

    def _apply_shading_toggle_to_cell(self, cell) -> None:
        smooth = self.render_mode_buttons.smooth_shading_enabled
        vao_list = self._chunk_gpu_objects.get(cell)
        cache_entries = self._chunk_normal_cache.get(cell)
        if not vao_list or not cache_entries or len(cache_entries) != len(vao_list):
            return
        for (
            _vao,
            vbo,
            _mat_name,
            _texture,
        ), (
            _cached_mat,
            positions,
            uvs,
            smooth_normals,
        ) in zip(vao_list, cache_entries):
            vbo.write(
                chunker.vertex_bytes_for_shading(
                    positions,
                    uvs,
                    smooth_normals,
                    smooth_shading=smooth,
                )
            )

    def _apply_shading_toggle(self) -> None:
        """
        Rewrites the normal columns of every currently-loaded chunk's VBO
        in place to match the current smooth_shading_enabled state -- no
        chunk reload or new GPU objects needed. The alternate payload is
        rebuilt only when the user toggles shading so loaded chunks do not
        retain both smooth and flat byte streams in RAM.
        """
        world = getattr(self, "world", None)
        if world is not None and hasattr(world, "set_prepack_smooth_shading"):
            world.set_prepack_smooth_shading(
                bool(self.render_mode_buttons.smooth_shading_enabled)
            )
        for cell in list(self._chunk_gpu_objects.keys()):
            self._apply_shading_toggle_to_cell(cell)

    def _buttons_locked_for_loading(self) -> bool:
        """True while map loading should disable the right-side button block."""
        if not self._has_map_loaded:
            return True
        if not self._initial_chunks_loaded:
            return True
        # Don't lock during the fade-out: textures must be re-enabled before
        # the dim overlay reveals the cave, otherwise the user sees gray
        # (untextured) geometry through the fading dim for the full 0.5 s fade.
        return (self.controls_overlay.is_active
                and not self.controls_overlay.is_manual_mode
                and not self.controls_overlay.is_fading)

    def _sync_render_mode_loading_policy(self) -> None:
        """Apply loading-time button policy and post-load defaults exactly on transitions."""
        locked = self._buttons_locked_for_loading()

        if locked:
            if self._render_mode_load_lock_active:
                return
            self.render_mode_buttons.texture_enabled = False
            self.render_mode_buttons.wireframe_enabled = False
            if self.render_mode_buttons.smooth_shading_enabled:
                self.render_mode_buttons.smooth_shading_enabled = False
                if self._has_map_loaded:
                    self._apply_shading_toggle()
            self._render_mode_load_lock_active = True
            return

        # Just unlocked after loading: enable only Texture.
        if self._render_mode_load_lock_active:
            self.render_mode_buttons.texture_enabled = True
            self.render_mode_buttons.wireframe_enabled = False
            if self.render_mode_buttons.smooth_shading_enabled:
                self.render_mode_buttons.smooth_shading_enabled = False
                if self._has_map_loaded:
                    self._apply_shading_toggle()
            self._render_mode_load_lock_active = False

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
    _CHUNK_PREP_MAX_FRACTION = 0.97
    _CHUNK_PREP_COMPLETE_HOLD_SECONDS = 0.85
    _STREAMING_FAILURES_PER_FRAME = 8

    def _target_streaming_load_radius(self) -> int:
        return int(self.render_distance_stepper.value)

    def _streaming_cell_priority_key(
        self,
    ) -> Callable[[tuple[int, int, int]], tuple[int, int, float, float, float]]:
        """Rank streaming cells by current camera view, then by distance.

        Render distance answers "how much cave should be eligible to load";
        this priority answers "which eligible cells should consume the next
        limited worker/upload slots."  A distance-only priority can spend that
        budget on nearby side/behind cells while the screen-facing corridor is
        still empty, which makes high distance values look ineffective.
        """
        world = getattr(self, "world", None)
        world_config = getattr(world, "config", None)
        chunk_size = max(1e-6, float(getattr(world_config, "chunk_size", 1.0)))
        position = np.asarray(self.camera.position, dtype=np.float64)
        forward = np.asarray(self.camera.forward(), dtype=np.float64)
        forward_norm = float(np.linalg.norm(forward))
        if forward_norm < 1e-9:
            forward = np.array([0.0, 0.0, -1.0], dtype=np.float64)
        else:
            forward = forward / forward_norm

        wnd = getattr(self, "wnd", None)
        window_size = getattr(wnd, "size", _DEFAULT_WINDOW_SIZE)
        width, height = window_size
        aspect = max(1.0, float(width) / max(1.0, float(height)))
        fov_deg = float(getattr(self.camera, "fov_deg", 75.0))
        half_fov_rad = math.radians(max(1.0, min(179.0, fov_deg)) * 0.5)
        visible_cone_tan = math.tan(half_fov_rad) * aspect * 1.25
        chunk_size_sq = chunk_size * chunk_size

        camera_x = float(position[0])
        camera_y = float(position[1])
        camera_z = float(position[2])
        forward_x = float(forward[0])
        forward_y = float(forward[1])
        forward_z = float(forward[2])

        def priority(cell: tuple[int, int, int]) -> tuple[int, int, float, float, float]:
            center_x = (cell[0] + 0.5) * chunk_size
            center_y = (cell[1] + 0.5) * chunk_size
            center_z = (cell[2] + 0.5) * chunk_size
            rel_x = center_x - camera_x
            rel_y = center_y - camera_y
            rel_z = center_z - camera_z
            depth = rel_x * forward_x + rel_y * forward_y + rel_z * forward_z
            distance_sq = rel_x * rel_x + rel_y * rel_y + rel_z * rel_z
            lateral_sq = max(0.0, distance_sq - depth * depth)
            front_penalty = 0 if depth >= -chunk_size else 1
            cone_depth = max(chunk_size, depth)
            visible_radius = cone_depth * visible_cone_tan + chunk_size
            visible_penalty = (
                0
                if front_penalty == 0 and lateral_sq <= visible_radius * visible_radius
                else 1
            )
            angular_sq = lateral_sq / max(chunk_size_sq, depth * depth)
            depth_cells = max(0.0, depth / chunk_size)
            distance_cells_sq = distance_sq / chunk_size_sq
            return (
                front_penalty,
                visible_penalty,
                depth_cells,
                angular_sq,
                distance_cells_sq,
            )

        return priority

    def _startup_upload_boost_is_active(self) -> bool:
        overlay = getattr(self, "controls_overlay", None)
        return (
            overlay is not None
            and overlay.is_waiting_for_begin
            and not getattr(self, "_initial_chunks_loaded", False)
        )

    def _streaming_upload_limits(self, stats: dict | None = None) -> tuple[int, int, float]:
        """Return chunk/operation/time upload limits for the current frame."""
        if self._startup_upload_boost_is_active():
            return (
                max(self._upload_chunks_per_frame, _STARTUP_UPLOAD_CHUNKS_PER_FRAME),
                max(
                    self._upload_groups_per_frame,
                    _STARTUP_UPLOAD_OPERATIONS_PER_CHUNK,
                ),
                max(self._upload_time_budget_ms, _STARTUP_UPLOAD_TIME_BUDGET_MS),
            )
        if stats is not None:
            ready = max(0, int(stats.get("ready", 0)))
            wanted = max(0, int(stats.get("wanted", 0)))
            loaded_wanted = max(
                0,
                int(stats.get("loaded_wanted", stats.get("loaded", 0))),
            )
            failed_wanted = max(0, int(stats.get("failed_wanted", 0)))
            missing_wanted = max(0, wanted - loaded_wanted - failed_wanted)
            if ready > 0 and missing_wanted > 0:
                return (
                    max(
                        self._upload_chunks_per_frame,
                        _CATCHUP_UPLOAD_CHUNKS_PER_FRAME,
                    ),
                    max(
                        self._upload_groups_per_frame,
                        _CATCHUP_UPLOAD_OPERATIONS_PER_CHUNK,
                    ),
                    max(
                        self._upload_time_budget_ms,
                        _CATCHUP_UPLOAD_TIME_BUDGET_MS,
                    ),
                )
        return (
            self._upload_chunks_per_frame,
            self._upload_groups_per_frame,
            self._upload_time_budget_ms,
        )

    @staticmethod
    def _initial_chunk_load_needed(
        stats: dict,
        max_loaded_chunks: int,
    ) -> int:
        total_available = max(1, int(stats.get("total_available", 1)))
        wanted = max(1, int(stats.get("wanted", CaveViewerWindow._INITIAL_LOAD_MIN_CHUNKS)))
        # Startup streams the same radius the viewer will reveal. Require that
        # current wanted set to settle before revealing the begin prompt;
        # otherwise the first visible frame can have missing chunk rectangles
        # beyond the old startup-only radius.
        return min(total_available, max(1, int(max_loaded_chunks)), wanted)

    def _initial_chunk_load_is_ready(self, stats: dict) -> bool:
        loaded = max(0, int(stats.get("loaded_wanted", stats.get("loaded", 0))))
        failed_wanted = max(0, int(stats.get("failed_wanted", 0)))
        max_loaded = max(1, int(getattr(self.world.config, "max_loaded_chunks", self._INITIAL_LOAD_MIN_CHUNKS)))
        needed = self._initial_chunk_load_needed(stats, max_loaded)
        return loaded + failed_wanted >= needed

    def _log_initial_compilation_complete(self, stats: dict) -> None:
        if getattr(self, "_initial_compilation_logged", False):
            return
        started_at = getattr(self, "_initial_compilation_started_at", None)
        if started_at is None:
            return

        elapsed_s = max(0.0, time.perf_counter() - started_at)
        self._initial_compilation_logged = True
        _LOG.info(
            "Initial map compilation completed in %.2fs "
            "(loaded=%d pending=%d ready=%d wanted=%d).",
            elapsed_s,
            int(stats.get("loaded", 0)),
            int(stats.get("pending", 0)),
            int(stats.get("ready", 0)),
            int(stats.get("wanted", 0)),
        )

    def _initial_chunk_load_progress(self, stats: dict) -> float:
        loaded = max(0, int(stats.get("loaded_wanted", stats.get("loaded", 0))))
        ready = max(0, int(stats.get("ready", 0)))
        pending = max(0, int(stats.get("pending", 0)))
        failed_wanted = max(0, int(stats.get("failed_wanted", 0)))
        max_loaded = max(1, int(getattr(self.world.config, "max_loaded_chunks", self._INITIAL_LOAD_MIN_CHUNKS)))
        needed = self._initial_chunk_load_needed(stats, max_loaded)
        # Give partial credit so the ring moves as soon as background
        # decode starts, not only once GPU uploads complete:
        #   pending  0.25  decode in progress
        #   ready    0.75  decode done, upload queued
        #   loaded   1.00  fully on GPU
        #   failed   1.00  terminally settled; render continues with a hole
        effective = loaded + failed_wanted + 0.75 * ready + 0.25 * min(pending, needed)
        return max(0.0, min(1.0, effective / needed))

    def _drain_streaming_worker_failures(self) -> None:
        world = getattr(self, "world", None)
        if world is None or not hasattr(world, "drain_worker_failures"):
            return
        for failure in world.drain_worker_failures(
            max_items=self._STREAMING_FAILURES_PER_FRAME
        ):
            log = _LOG.error if failure.fatal else _LOG.warning
            log(
                "Streaming worker %s for chunk %s during %s on %s: %s: %s",
                "failed" if failure.fatal else "reported a non-fatal failure",
                failure.cell,
                failure.stage,
                failure.thread_name,
                failure.error_type,
                failure.message,
            )

    @staticmethod
    def _frustum_planes(view: np.ndarray, proj: np.ndarray) -> np.ndarray:
        """
        Extract the 6 view-frustum planes in world space from the row-major
        view and projection matrices using the Gribb-Hartmann method.
        The combined clip matrix M = proj @ view maps world-space column
        vectors to clip space; summing/differencing rows of M gives the 6
        plane equations. Returns a (6, 4) float64 array where each row
        (a, b, c, d) satisfies a*x + b*y + c*z + d >= 0 for inside points.
        """
        vp = (proj @ view).astype(np.float64)
        planes = np.empty((6, 4), dtype=np.float64)
        planes[0] = vp[3] + vp[0]   # left
        planes[1] = vp[3] - vp[0]   # right
        planes[2] = vp[3] + vp[1]   # bottom
        planes[3] = vp[3] - vp[1]   # top
        planes[4] = vp[3] + vp[2]   # near
        planes[5] = vp[3] - vp[2]   # far
        lengths = np.linalg.norm(planes[:, :3], axis=1, keepdims=True)
        planes /= np.maximum(lengths, 1e-9)
        return planes

    @staticmethod
    def _aabb_inside_frustum(planes: np.ndarray,
                              bmin: np.ndarray, bmax: np.ndarray) -> bool:
        """
        Positive-vertex frustum-AABB test. For each plane, pick the AABB
        corner furthest along the plane normal (the 'positive vertex'). If
        that corner is outside the plane, the entire AABB is outside the
        frustum (conservative -- produces no false culls).
        """
        for a, b, c, d in planes:
            px = bmax[0] if a >= 0 else bmin[0]
            py = bmax[1] if b >= 0 else bmin[1]
            pz = bmax[2] if c >= 0 else bmin[2]
            if a * px + b * py + c * pz + d < 0:
                return False
        return True

    def _right_column_ui_scale(self) -> float:
        return float(getattr(self, "_viewer_ui_scale", 1.0))

    def _right_column_geometry_scale(self) -> float:
        return float(
            getattr(self, "_right_column_panel_scale", self.RIGHT_COLUMN_PANEL_SCALE)
        )

    def _right_column_text_scale(self) -> float:
        return float(
            getattr(
                self,
                "_right_column_panel_text_scale",
                self.RIGHT_COLUMN_PANEL_TEXT_SCALE,
            )
        )

    def _right_column_label_text_scale(self) -> float:
        return float(
            getattr(
                self,
                "_right_column_panel_label_text_scale",
                self.RIGHT_COLUMN_PANEL_LABEL_TEXT_SCALE,
            )
        )

    def _right_column_button_text_scale(self) -> float:
        return float(
            getattr(
                self,
                "_right_column_panel_button_text_scale",
                self.RIGHT_COLUMN_PANEL_BUTTON_TEXT_SCALE,
            )
        )

    def _update_right_column_hud_scale(self, window_size: tuple[int, int]) -> None:
        """Keep the always-visible HUD legible as the viewer is resized."""
        viewer_ui_scale = _viewer_ui_scale_for_window_size(window_size)
        geometry_scale = self.RIGHT_COLUMN_PANEL_SCALE * viewer_ui_scale
        text_scale = (
            self.RIGHT_COLUMN_PANEL_TEXT_SCALE
            * min(viewer_ui_scale, self.RIGHT_COLUMN_PANEL_TEXT_MAX_UI_SCALE)
        )
        label_text_scale = (
            self.RIGHT_COLUMN_PANEL_LABEL_TEXT_SCALE
            * min(viewer_ui_scale, self.RIGHT_COLUMN_PANEL_TEXT_MAX_UI_SCALE)
        )
        button_text_scale = (
            self.RIGHT_COLUMN_PANEL_BUTTON_TEXT_SCALE
            * min(viewer_ui_scale, self.RIGHT_COLUMN_PANEL_TEXT_MAX_UI_SCALE)
        )
        if (
            viewer_ui_scale == self._right_column_ui_scale()
            and geometry_scale == self._right_column_geometry_scale()
            and text_scale == self._right_column_text_scale()
            and label_text_scale == self._right_column_label_text_scale()
            and button_text_scale == self._right_column_button_text_scale()
        ):
            return

        self._viewer_ui_scale = viewer_ui_scale
        self._right_column_panel_scale = geometry_scale
        self._right_column_panel_text_scale = text_scale
        self._right_column_panel_label_text_scale = label_text_scale
        self._right_column_panel_button_text_scale = button_text_scale
        self._layout_cache_size = None
        self._layout_cache_result = None

        for control in (
            getattr(self, "light_stepper", None),
            getattr(self, "ambient_stepper", None),
            getattr(self, "render_distance_stepper", None),
        ):
            setter = getattr(control, "set_scale", None)
            if callable(setter):
                setter(
                    text_scale=text_scale,
                    geometry_scale=geometry_scale,
                    label_text_scale=label_text_scale,
                )

        setter = getattr(getattr(self, "render_mode_buttons", None), "set_scale", None)
        if callable(setter):
            setter(text_scale=button_text_scale, geometry_scale=geometry_scale)

    def _right_column_layout(self, window_size: tuple[int, int]) -> dict:
        """
        Returns a dict with every position the right-side column needs:
        'brightness_anchor', 'ambient_anchor' (the GLOBAL LIGHT stepper),
        'render_distance_anchor' (note: this stepper moved to the right
        column per request, no longer on the left), and 'buttons_top_y'
        -- each stepper anchor already accounts for its own label space
        above it (see StepperControl.render's label_above handling), and
        the button block's top_y already accounts for RenderModeButtons'
        own height-shrinking safety net on short windows.

        Stack order, top to bottom: Brightness, Global Light, Render
        Distance, then the button block.
        """
        self._update_right_column_hud_scale(window_size)
        if window_size == self._layout_cache_size:
            return self._layout_cache_result

        w, h = window_size

        # Label reserve matches StepperControl.render's own label metrics so
        # this stays correct if that label styling ever changes (rather
        # than a second hard-coded guess at the same number).
        from caveviewer.gui import bitmap_font
        panel_scale = self._right_column_geometry_scale()
        panel_label_text_scale = self._right_column_label_text_scale()
        viewer_ui_scale = self._right_column_ui_scale()
        fixed_label_size = bitmap_font.pixel_size_at_text_scale(
            StepperControl.LABEL_TEXT_SIZE,
            StepperControl.FIXED_TEXT_SCALE * panel_label_text_scale,
        )
        label_reserve = bitmap_font.text_height_px(fixed_label_size) + 8 * panel_scale

        button_block_height = RenderModeButtons.total_stack_height(scale=panel_scale)
        content_right_inset = (
            self.RIGHT_COLUMN_PANEL_RIGHT_MARGIN + self.RIGHT_COLUMN_PANEL_SIDE_PAD
        ) * viewer_ui_scale
        content_bottom_inset = (
            self.RIGHT_COLUMN_PANEL_BOTTOM_MARGIN + self.RIGHT_COLUMN_PANEL_BOTTOM_PAD
        ) * viewer_ui_scale

        # Build the stack from the BOTTOM up: button block's bottom sits
        # RIGHT_COLUMN_BOTTOM_MARGIN above the window's bottom edge.
        buttons_bottom_y = h - content_bottom_inset
        buttons_top_y = buttons_bottom_y - button_block_height

        render_distance_bottom_y = buttons_top_y - self.RIGHT_COLUMN_BUTTON_GROUP_GAP * panel_scale
        render_distance_anchor_y = render_distance_bottom_y - self.render_distance_stepper.total_height()

        ambient_bottom_y = render_distance_anchor_y - label_reserve - self.RIGHT_COLUMN_GAP * panel_scale
        ambient_anchor_y = ambient_bottom_y - self.ambient_stepper.total_height()

        brightness_bottom_y = ambient_anchor_y - label_reserve - self.RIGHT_COLUMN_GAP * panel_scale
        brightness_anchor_y = brightness_bottom_y - self.light_stepper.total_height()

        right_x_brightness = w - content_right_inset - self.light_stepper.total_width()
        right_x_ambient = w - content_right_inset - self.ambient_stepper.total_width()
        right_x_render_distance = w - content_right_inset - self.render_distance_stepper.total_width()

        result = {
            "brightness_anchor": (right_x_brightness, brightness_anchor_y),
            "ambient_anchor": (right_x_ambient, ambient_anchor_y),
            "render_distance_anchor": (right_x_render_distance, render_distance_anchor_y),
            "buttons_top_y": buttons_top_y,
            "content_right_inset": content_right_inset,
            "content_bottom_inset": content_bottom_inset,
        }
        self._layout_cache_size = window_size
        self._layout_cache_result = result
        return result

    def _right_column_panel_rect(self, window_size: tuple[int, int], column: dict | None = None) -> tuple[float, float, float, float]:
        """Bounds for the shared backplate behind the right-side HUD column."""
        if column is None:
            column = self._right_column_layout(window_size)

        w, h = window_size
        fixed_label_size = bitmap_font.pixel_size_at_text_scale(
            self.RIGHT_COLUMN_PANEL_LABEL_SIZE,
            StepperControl.FIXED_TEXT_SCALE * self._right_column_label_text_scale(),
        )
        label_height = bitmap_font.text_height_px(fixed_label_size)
        label_widths = [
            bitmap_font.text_width_px(self.light_stepper.label, fixed_label_size),
            bitmap_font.text_width_px(self.ambient_stepper.label, fixed_label_size),
            bitmap_font.text_width_px(
                self.render_distance_stepper.label,
                fixed_label_size,
            ),
        ]
        buttons_top_y = column["buttons_top_y"]

        brightness_anchor_x, brightness_anchor_y = column["brightness_anchor"]
        ambient_anchor_x, ambient_anchor_y = column["ambient_anchor"]
        render_distance_anchor_x, render_distance_anchor_y = column["render_distance_anchor"]

        stepper_lefts = [brightness_anchor_x, ambient_anchor_x, render_distance_anchor_x]
        stepper_rights = [
            brightness_anchor_x + self.light_stepper.total_width(),
            ambient_anchor_x + self.ambient_stepper.total_width(),
            render_distance_anchor_x + self.render_distance_stepper.total_width(),
        ]
        stepper_widths = [
            self.light_stepper.total_width(),
            self.ambient_stepper.total_width(),
            self.render_distance_stepper.total_width(),
        ]
        label_lefts = [
            anchor_x + (stepper_width - label_width) / 2.0
            for anchor_x, stepper_width, label_width in zip(
                stepper_lefts,
                stepper_widths,
                label_widths,
            )
        ]
        panel_scale = self._right_column_geometry_scale()
        viewer_ui_scale = self._right_column_ui_scale()
        label_gap = self.RIGHT_COLUMN_PANEL_LABEL_GAP * panel_scale
        label_tops = [
            brightness_anchor_y - label_height - label_gap,
            ambient_anchor_y - label_height - label_gap,
            render_distance_anchor_y - label_height - label_gap,
        ]

        button_x0, _button_y0, button_x1, _button_y1 = self.render_mode_buttons._button_rect_px(
            0, window_size, buttons_top_y, column["content_right_inset"]
        )
        _last_x0, _last_y0, _last_x1, button_bottom_y = self.render_mode_buttons._button_rect_px(
            6, window_size, buttons_top_y, column["content_right_inset"]
        )

        x0 = min(min(stepper_lefts), min(label_lefts), button_x0) - (
            self.RIGHT_COLUMN_PANEL_SIDE_PAD * viewer_ui_scale
        )
        x1 = w - (self.RIGHT_COLUMN_PANEL_RIGHT_MARGIN * viewer_ui_scale)
        y0 = min(label_tops) - (self.RIGHT_COLUMN_PANEL_TOP_PAD * viewer_ui_scale)
        y1 = h - (self.RIGHT_COLUMN_PANEL_BOTTOM_MARGIN * viewer_ui_scale)
        return (x0, y0, x1, y1)

    def _render_right_column_panel(self, window_size: tuple[int, int], column: dict | None = None) -> None:
        """Draw a shared translucent panel behind the right-side HUD controls."""
        if column is None:
            column = self._right_column_layout(window_size)

        x0, y0, x1, y1 = self._right_column_panel_rect(window_size, column)
        w, h = window_size
        verts = []

        def px_to_ndc(x: float, y: float) -> tuple[float, float]:
            nx = (x / w) * 2.0 - 1.0
            ny = 1.0 - (y / h) * 2.0
            return nx, ny

        def add_quad_px(qx0: float, qy0: float, qx1: float, qy1: float, rgba: tuple[float, float, float, float]) -> None:
            nx0, ny0 = px_to_ndc(qx0, qy0)
            nx1, ny1 = px_to_ndc(qx1, qy1)
            top, bottom = max(ny0, ny1), min(ny0, ny1)
            left, right = min(nx0, nx1), max(nx0, nx1)
            quad = [
                (left, bottom), (right, bottom), (right, top),
                (left, bottom), (right, top), (left, top),
            ]
            for vx, vy in quad:
                verts.append((vx, vy, *rgba))

        add_quad_px(x0, y0, x1, y1, self.RIGHT_COLUMN_PANEL_FILL_RGBA)

        border = self.RIGHT_COLUMN_PANEL_BORDER_PX
        border_color = self.RIGHT_COLUMN_PANEL_BORDER_RGBA
        add_quad_px(x0, y0, x1, y0 + border, border_color)
        add_quad_px(x0, y1 - border, x1, y1, border_color)
        add_quad_px(x0, y0, x0 + border, y1, border_color)
        add_quad_px(x1 - border, y0, x1, y1, border_color)

        data = np.array(verts, dtype=np.float32)
        self._hud_panel_vbo.write(data.tobytes())

        self.ctx.disable(moderngl.CULL_FACE)
        self.ctx.disable(moderngl.DEPTH_TEST)
        self.ctx.enable(moderngl.BLEND)
        self._hud_panel_vao.render(moderngl.TRIANGLES, vertices=len(verts))
        self.ctx.disable(moderngl.BLEND)
        self.ctx.enable(moderngl.DEPTH_TEST)
        self.ctx.enable(moderngl.CULL_FACE)

    def _render_minimap(self, window_size: tuple[int, int]) -> None:
        """Draw the minimap in the normal HUD, keeping it out of recordings."""
        if self.minimap is not None:
            self.minimap.render(window_size, self.camera.position, self.camera.forward(),
                                self._bookmarks)

    def _render_recording_status_message(self, window_size: tuple[int, int]) -> None:
        now = time.perf_counter()
        status = self._ensure_recording_controller().active_status(now=now)
        if status is None:
            return

        message = status.message
        detail = status.detail
        kind = status.kind

        w, h = window_size
        self._render_recording_countdown_scrim(window_size, alpha=0.42)

        symbol = {
            "success": "OK",
            "error": "!",
            "cancel": "X",
        }.get(kind, "OK")
        symbol_size = 5.2 if symbol == "OK" else 7.2
        center_x = w / 2.0
        ring_center_y = h / 2.0 - 54.0
        self.import_progress_panel.draw_ring_label(
            center_x=center_x,
            center_y=ring_center_y,
            window_size=window_size,
            label=symbol,
            progress=1.0,
            pixel_size=symbol_size,
            fixed_text_scale=self.UI_TEXT_SCALE,
        )

        verts = []

        def px_to_ndc(x: float, y: float) -> tuple[float, float]:
            return (x / w) * 2.0 - 1.0, 1.0 - (y / h) * 2.0

        def add_quad_px(qx0: float, qy0: float, qx1: float, qy1: float,
                        rgba: tuple[float, float, float, float]) -> None:
            nx0, ny0 = px_to_ndc(qx0, qy0)
            nx1, ny1 = px_to_ndc(qx1, qy1)
            top, bottom = max(ny0, ny1), min(ny0, ny1)
            left, right = min(nx0, nx1), max(nx0, nx1)
            quad = [
                (left, bottom), (right, bottom), (right, top),
                (left, bottom), (right, top), (left, top),
            ]
            for vx, vy in quad:
                verts.append((vx, vy, *rgba))

        def add_centered_text(
            text: str,
            y: float,
            pixel_size: float,
            rgba: tuple[float, float, float, float],
        ) -> float:
            pixel_size = bitmap_font.pixel_size_at_text_scale(pixel_size, self.UI_TEXT_SCALE)
            min_pixel_size = bitmap_font.pixel_size_at_text_scale(1.35, self.UI_TEXT_SCALE)
            bounds = bitmap_font.text_bounds_px(text, pixel_size)
            text_w = bounds[2] - bounds[0]
            max_w = max(120.0, w - 96.0)
            if text_w > max_w:
                pixel_size = max(min_pixel_size, pixel_size * max_w / text_w)
                bounds = bitmap_font.text_bounds_px(text, pixel_size)
                text_w = bounds[2] - bounds[0]
            text_h = bounds[3] - bounds[1]
            origin_x = (w - text_w) / 2.0 - bounds[0]
            origin_y = y - bounds[1]
            r, g, b, a = rgba
            for glyph in bitmap_font.iter_text_pixels(text, origin_x, origin_y, pixel_size):
                px0, py0, px1, py1 = glyph[0], glyph[1], glyph[2], glyph[3]
                glyph_alpha = glyph[4] if len(glyph) > 4 else 1.0
                add_quad_px(px0, py0, px1, py1, (r, g, b, a * glyph_alpha))
            return text_h

        message_y = ring_center_y + 86.0
        main_color = (0.8980, 0.6314, 0.1216, 1.0)
        detail_color = (0.835, 0.855, 0.86, 0.88)
        main_h = add_centered_text(message, message_y, 2.9, main_color)
        if detail:
            add_centered_text(detail, message_y + main_h + 18.0, 1.65, detail_color)

        data = np.array(verts, dtype=np.float32)
        if len(verts) > self._status_panel_max_verts:
            self._status_panel_vbo.release()
            self._status_panel_max_verts = max(self._status_panel_max_verts * 2, len(verts))
            self._status_panel_vbo = self.ctx.buffer(reserve=self._status_panel_max_verts * 6 * 4)
            self._status_panel_vao = self.ctx.vertex_array(
                self._hud_panel_program,
                [(self._status_panel_vbo, "2f 4f", "in_pos", "in_color")],
            )

        self._status_panel_vbo.write(data.tobytes())
        self.ctx.disable(moderngl.CULL_FACE)
        self.ctx.disable(moderngl.DEPTH_TEST)
        self.ctx.enable(moderngl.BLEND)
        self._status_panel_vao.render(moderngl.TRIANGLES, vertices=len(verts))
        self.ctx.disable(moderngl.BLEND)
        self.ctx.enable(moderngl.DEPTH_TEST)
        self.ctx.enable(moderngl.CULL_FACE)

    def _render_recording_countdown_scrim(self, window_size: tuple[int, int], alpha: float = 0.62) -> None:
        """Darken the cave view behind the countdown ring without hiding it."""
        w, h = window_size
        verts = []

        def px_to_ndc(x: float, y: float) -> tuple[float, float]:
            return (x / w) * 2.0 - 1.0, 1.0 - (y / h) * 2.0

        def add_quad_px(qx0: float, qy0: float, qx1: float, qy1: float,
                        rgba: tuple[float, float, float, float]) -> None:
            nx0, ny0 = px_to_ndc(qx0, qy0)
            nx1, ny1 = px_to_ndc(qx1, qy1)
            top, bottom = max(ny0, ny1), min(ny0, ny1)
            left, right = min(nx0, nx1), max(nx0, nx1)
            quad = [
                (left, bottom), (right, bottom), (right, top),
                (left, bottom), (right, top), (left, top),
            ]
            for vx, vy in quad:
                verts.append((vx, vy, *rgba))

        add_quad_px(0, 0, w, h, (0.001, 0.002, 0.005, alpha))
        data = np.array(verts, dtype=np.float32)
        self._status_panel_vbo.write(data.tobytes())

        self.ctx.disable(moderngl.CULL_FACE)
        self.ctx.disable(moderngl.DEPTH_TEST)
        self.ctx.enable(moderngl.BLEND)
        self._status_panel_vao.render(moderngl.TRIANGLES, vertices=len(verts))
        self.ctx.disable(moderngl.BLEND)
        self.ctx.enable(moderngl.DEPTH_TEST)
        self.ctx.enable(moderngl.CULL_FACE)

    def _render_throttle_due(self, key: str, interval_s: float) -> bool:
        """
        Return true when a low-value render state should draw this callback.

        Some early-return states, such as a minimized window, do not need
        full-speed work. Use timestamp gates rather than sleeping in the render
        callback so the backend/UI thread remains available for window events
        and queued task results.
        """
        due_at = getattr(self, "_render_throttle_due_at", None)
        if due_at is None:
            due_at = {}
            self._render_throttle_due_at = due_at

        now = time.perf_counter()
        if now < due_at.get(key, 0.0):
            return False

        due_at[key] = now + max(0.0, interval_s)
        return True

    def _reset_render_throttle(self, *keys: str) -> None:
        """Forget throttle deadlines for states that are no longer active."""
        due_at = getattr(self, "_render_throttle_due_at", None)
        if not due_at:
            return
        if not keys:
            due_at.clear()
            return
        for key in keys:
            due_at.pop(key, None)

    def on_render(self, current_time: float, frame_time: float):
        if not getattr(self, "_window_setup_complete", False):
            return
        if self._closing_requested:
            return

        # Backends can miss iconify callbacks on Dock minimize; poll a
        # few common window flags each frame as a safety net.
        runtime_iconified = self._query_runtime_iconified_state()
        self._set_background_pause(runtime_iconified, "runtime window state")

        if self._is_iconified:
            # Keep minimize mode cheap: no streaming updates/uploads while
            # iconified.  Poll low-frequency completion state without blocking
            # the render/window callback.
            if self._render_throttle_due(
                "iconified", _ICONIFIED_RENDER_POLL_INTERVAL_S
            ):
                self._drain_recording_stop_results()
            return
        self._reset_render_throttle("iconified")

        bitmap_font.set_raster_scale(_window_pixel_ratio(self.wnd))

        # Keep render-mode button effects synced to loading state even
        # on frames that early-return before normal HUD interaction.
        self._sync_render_mode_loading_policy()
        self._drain_recording_stop_results()

        if self._startup_focus_enabled:
            self._request_startup_focus_once()

        # Background import in flight: drain worker results on every callback
        # and redraw the progress panel every callback. Window backends may
        # still present/swap after this method returns, so skipping draws here
        # can expose stale back buffers as visible flicker during first-time
        # imports.
        if self._import_active:
            self._drain_import_queue()
            if not self._import_active:
                return
            self.ctx.clear(0.02, 0.02, 0.03)
            fraction = self._import_progress_fraction
            # When the real fraction is near zero (numpy is crunching
            # faces and can't report sub-step progress), pulse the ring
            # gently between 0 and 2 % so it looks alive.  The pulse is
            # capped below the first real progress step (3 %) so the
            # max() inside import_progress_panel takes over cleanly once
            # measurable progress begins.
            if fraction < 0.021:
                t = time.perf_counter()
                fraction = abs(math.sin(t * 1.2)) * 0.02
            self.import_progress_panel.render(
                self.wnd.size, self._import_map_name,
                self._import_progress_stage, fraction,
                title=self._import_progress_title,
                note=self._import_progress_note,
            )
            return
        self._reset_render_throttle("import_progress")

        if not self._has_map_loaded:
            if self._render_throttle_due(
                "import_pause_notice", _IMPORT_PAUSE_NOTICE_RENDER_INTERVAL_S
            ):
                if self._render_import_pause_notice_if_active():
                    return
                self._reset_render_throttle("import_pause_notice")
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

        frame_start = time.perf_counter()

        # Sleep/wake (or a debugger stop) can yield a very large frame_time
        # and leave input/capture state stale (e.g. key-release never seen).
        # Reset transient input flags on these discontinuities.
        if frame_time > 2.0:
            self._reset_transient_input_state("long frame gap")

        t_input = time.perf_counter()
        dt = max(frame_time, 1e-4)
        self._handle_continuous_input(dt)
        input_ms = (time.perf_counter() - t_input) * 1000.0

        # Apply the render-distance control's current value before the
        # streaming world recalculates this frame -- a click on +/- takes
        # effect immediately rather than waiting for the camera to move
        # (see the matching check in StreamingWorld.update(), which
        # detects a changed load_radius_cells on its own, not just a
        # moved camera -- this assignment is what actually gives it a
        # changed value to detect).
        target_load_radius = self._target_streaming_load_radius()
        if self.world.config.load_radius_cells != target_load_radius:
            self.world.config.load_radius_cells = target_load_radius

        t0 = time.perf_counter()
        streaming_timing = self._new_streaming_frame_timing()
        self._streaming_frame_timing = streaming_timing
        try:
            t_update = time.perf_counter()
            self.world.update(
                self.camera.position.astype(np.float32),
                cell_priority_key=self._streaming_cell_priority_key(),
            )
            streaming_timing["update_ms"] = (time.perf_counter() - t_update) * 1000.0

            pre_drain_stats = self.world.stats()
            (
                upload_chunks_per_frame,
                upload_operations_per_chunk,
                upload_time_budget_ms,
            ) = self._streaming_upload_limits(pre_drain_stats)
            self._current_upload_operations_per_chunk = upload_operations_per_chunk
            self._current_upload_time_budget_ms = upload_time_budget_ms
            t_drain = time.perf_counter()
            t_ready_drain = time.perf_counter()
            self.world.drain_ready_chunks(
                self._on_chunk_ready, self._on_chunk_unload,
                max_per_frame=upload_chunks_per_frame,
                time_budget_ms=upload_time_budget_ms,
            )
            streaming_timing["ready_drain_ms"] = (
                time.perf_counter() - t_ready_drain
            ) * 1000.0
            t_failure_drain = time.perf_counter()
            self._drain_streaming_worker_failures()
            streaming_timing["failure_drain_ms"] = (
                time.perf_counter() - t_failure_drain
            ) * 1000.0
            streaming_timing["drain_ms"] = (time.perf_counter() - t_drain) * 1000.0
            self._record_upload_slice_sizes(streaming_timing)
        finally:
            self._streaming_frame_timing = None
        streaming_ms = (time.perf_counter() - t0) * 1000.0
        stats = self.world.stats()
        if not self._initial_chunks_loaded and self._initial_chunk_load_is_ready(stats):
            self._initial_chunks_loaded = True
            self._log_initial_compilation_complete(stats)

        # As soon as prep crosses the readiness threshold, hold a brief
        # fully-complete frame so the progress bar doesn't disappear abruptly.
        if self._initial_chunks_loaded and not self._chunk_prep_completion_armed:
            self._chunk_prep_completion_armed = True
            self._chunk_prep_complete_until = (
                time.perf_counter() + self._CHUNK_PREP_COMPLETE_HOLD_SECONDS
            )

        # Show a loading indicator while the initial chunks stream in from disk.
        # Without this the screen is black until the first chunk arrives, which
        # can take several seconds on slow hardware or large maps.
        now = time.perf_counter()
        if not self._initial_chunks_loaded:
            _map_name = os.path.basename(self.manifest.get("source_obj", "map"))
            raw_fraction = self._initial_chunk_load_progress(stats)
            target = min(self._CHUNK_PREP_MAX_FRACTION, raw_fraction * self._CHUNK_PREP_MAX_FRACTION)
            self._chunk_prep_progress = max(self._chunk_prep_progress, target)
            self.import_progress_panel.render(
                self.wnd.size, _map_name, "opening cave", self._chunk_prep_progress,
                title="", note="",
            )
            return

        if self._chunk_prep_complete_until is not None and now < self._chunk_prep_complete_until:
            _map_name = os.path.basename(self.manifest.get("source_obj", "map"))
            self.import_progress_panel.render(
                self.wnd.size, _map_name, "opening cave", 1.0,
                title="", note="",
            )
            return

        self._chunk_prep_complete_until = None

        t_scene_setup = time.perf_counter()
        self.ctx.clear(*self.color_picker.color)  # background ("void") color, adjustable via the COLOR button

        aspect = self.wnd.size[0] / max(self.wnd.size[1], 1)
        view = self.camera.view_matrix()
        proj = self.camera.projection_matrix(aspect)

        self.program["u_view"].write(view.T.tobytes())
        self.program["u_projection"].write(proj.T.tobytes())
        _pos = self.camera.position
        self.program["u_camera_pos"].value = (float(_pos[0]), float(_pos[1]), float(_pos[2]))
        self.program["u_light_color"].value = (1.0, 0.95, 0.85)  # warm headlamp tone
        self.program["u_light_intensity"].value = float(self.light_stepper.value)
        # GLOBAL LIGHT stepper (0-10) maps linearly onto the shader's
        # actual ambient range -- see _AMBIENT_MIN/_AMBIENT_MAX's
        # docstring above for why 0 reproduces the app's original fixed
        # ambient value rather than true darkness.
        ambient_t = self.ambient_stepper.value / self.ambient_stepper.max_value
        ambient_value = self._AMBIENT_MIN + ambient_t * (self._AMBIENT_MAX - self._AMBIENT_MIN)
        self.program["u_ambient"].value = ambient_value
        self.program["u_texture_enabled"].value = self.render_mode_buttons.texture_enabled
        scene_setup_ms = (time.perf_counter() - t_scene_setup) * 1000.0

        t0 = time.perf_counter()

        # Solid pass (textured, or plain gray if Texture is off) only
        # draws when at least one of "show texture" or "wireframe is off"
        # is true. In other words: skip the solid pass entirely when the
        # person has explicitly turned Texture off AND turned Mesh
        # (wireframe) on -- that combination means "show me pure
        # wireframe, nothing else", and the solid pass would otherwise
        # always render underneath the wireframe lines regardless of the
        # Texture toggle, which defeats the point of turning texture off
        # in the first place when inspecting wireframe-only.
        show_solid_pass = self.render_mode_buttons.texture_enabled or not self.render_mode_buttons.wireframe_enabled

        # Frustum-cull loaded chunks against the current view before drawing.
        # Build _visible_cells once so both solid and wireframe passes share
        # the same culled set without repeating the test.
        t_cull = time.perf_counter()
        _vp_planes = self._frustum_planes(view, proj)
        _visible_cells = []
        for cell, vao_list in self._chunk_gpu_objects.items():
            aabb = self._chunk_aabbs.get(cell)
            if aabb is None or self._aabb_inside_frustum(_vp_planes, aabb[0], aabb[1]):
                _visible_cells.append((cell, vao_list))
        _chunks_drawn = len(_visible_cells)
        mesh_cull_ms = (time.perf_counter() - t_cull) * 1000.0

        # u_texture always refers to sampler unit 0 -- set it once before
        # the loop rather than redundantly on every single draw call.
        def _draw_visible_mesh() -> None:
            self.program["u_texture"].value = 0
            if show_solid_pass:
                for cell, vao_list in _visible_cells:
                    for vao, vbo, mat_name, texture in vao_list:
                        texture.use(location=0)
                        vao.render(moderngl.TRIANGLES)

            # Wireframe pass: drawn whenever Mesh is toggled on. If the solid
            # pass also drew (texture or gray surface visible), this overlays
            # triangulation on top of it. If the solid pass was skipped (the
            # texture-off + wireframe-on combination above), this is the only
            # thing that draws -- true wireframe-only.
            if self.render_mode_buttons.wireframe_enabled:
                # NOTE: this draws coincident wireframe lines directly on top of
                # the solid pass's geometry, which can show minor z-fighting/
                # flicker on some GPUs since both passes write near-identical
                # depth values. A polygon-offset bias would clean this up, but
                # since the bias amount needs hand-tuning against moderngl's
                # actual ctx.polygon_offset API (left out here rather than
                # guess at a value that could silently do nothing or look
                # wrong), this is a known minor cosmetic rough edge -- the
                # wireframe is still fully readable, just not perfectly crisp
                # in rare cases.
                self.ctx.wireframe = True
                for cell, vao_list in _visible_cells:
                    for vao, vbo, mat_name, texture in vao_list:
                        vao.render(moderngl.TRIANGLES)
                self.ctx.wireframe = False

        mesh_gpu_query_wait_ms = 0.0
        t_submit = time.perf_counter()
        if self._gpu_draw_timer_enabled:
            # GPU timer queries are useful diagnostics but reading the result
            # in the same frame can block until the driver has completed the
            # measured work. Keep that synchronization out of normal viewing;
            # enable CAVEVIEWER_GPU_DRAW_TIMER=1 only while actively measuring
            # GPU-side draw cost.
            with self.ctx.query(time=True) as _gpu_q:
                _draw_visible_mesh()
            mesh_submit_ms = (time.perf_counter() - t_submit) * 1000.0
            t_query_wait = time.perf_counter()
            self._last_gpu_draw_ms = _gpu_q.elapsed / 1_000_000
            mesh_gpu_query_wait_ms = (time.perf_counter() - t_query_wait) * 1000.0
        else:
            self._last_gpu_draw_ms = None
            _draw_visible_mesh()
            mesh_submit_ms = (time.perf_counter() - t_submit) * 1000.0
        mesh_draw_ms = (time.perf_counter() - t0) * 1000.0

        def _render_recording_frame(
            framebuffer: moderngl.Framebuffer,
            output_size: tuple[int, int],
        ) -> None:
            output_width, output_height = output_size
            previous_fbo = getattr(self.ctx, "fbo", None)
            previous_screen_viewport = getattr(self.ctx.screen, "viewport", None)
            previous_framebuffer_viewport = getattr(framebuffer, "viewport", None)
            recording_proj = self.camera.projection_matrix(
                output_width / max(output_height, 1)
            )
            try:
                framebuffer.use()
                framebuffer.viewport = (0, 0, output_width, output_height)
                self.ctx.clear(*self.color_picker.color)
                self.program["u_projection"].write(recording_proj.T.tobytes())
                self.program["u_view"].write(view.T.tobytes())
                _draw_visible_mesh()
            finally:
                try:
                    if previous_fbo is not None:
                        previous_fbo.use()
                    else:
                        self.ctx.screen.use()
                except Exception:
                    try:
                        self.ctx.screen.use()
                    except Exception:
                        pass
                if previous_screen_viewport is not None:
                    try:
                        self.ctx.screen.viewport = previous_screen_viewport
                    except Exception:
                        pass
                if previous_framebuffer_viewport is not None:
                    try:
                        framebuffer.viewport = previous_framebuffer_viewport
                    except Exception:
                        pass
                self.ctx.wireframe = False
                self.program["u_projection"].write(proj.T.tobytes())
                self.program["u_view"].write(view.T.tobytes())

        recording_read_ms = 0.0
        recording_stage_ms = 0.0
        recording_drain_ms = 0.0
        if self._recording_hides_hud():
            now = time.perf_counter()
            if self._recording_countdown_until is not None and now < self._recording_countdown_until:
                countdown_number, countdown_progress = self._recording_countdown_display(now)
                self._render_recording_countdown_scrim(self.wnd.size)
                self.import_progress_panel.draw_countdown_number(
                    center_x=self.wnd.size[0] / 2.0,
                    center_y=self.wnd.size[1] / 2.0,
                    window_size=self.wnd.size,
                    number=countdown_number,
                    progress=countdown_progress,
                    fixed_text_scale=self.UI_TEXT_SCALE,
                )
            else:
                recording_read_ms = self._recording_update_after_scene(
                    now,
                    render_frame=_render_recording_frame,
                )
                recording_stage_ms = self._recording_last_stage_ms
                recording_drain_ms = self._recording_last_drain_ms
            overlay_ms = 0.0
        else:
            # Overlay HUD elements draw last, on top of the 3D scene, each with
            # their own depth-disabled 2D pass.
            t0 = time.perf_counter()

            # Whole right-side column -- brightness, global light, render
            # distance, then the Mesh/Texture/Help/Color/Open/Rec buttons -- is
            # laid out as one group anchored to the bottom-right corner. See
            # _right_column_layout()'s docstring for why this is computed in
            # one place rather than each piece anchoring itself independently.
            column = self._right_column_layout(self.wnd.size)
            brightness_anchor_x, brightness_anchor_y = column["brightness_anchor"]
            ambient_anchor_x, ambient_anchor_y = column["ambient_anchor"]
            render_distance_anchor_x, render_distance_anchor_y = column["render_distance_anchor"]
            buttons_top_y = column["buttons_top_y"]

            self._render_right_column_panel(self.wnd.size, column)
            self.light_stepper.render(self.wnd.size, brightness_anchor_x, brightness_anchor_y, label_above=True)
            self.ambient_stepper.render(self.wnd.size, ambient_anchor_x, ambient_anchor_y, label_above=True)
            self.render_distance_stepper.render(self.wnd.size, render_distance_anchor_x, render_distance_anchor_y,
                                                label_above=True)

            self._render_minimap(self.wnd.size)

            self.render_mode_buttons.render(self.wnd.size, buttons_top_y,
                              help_active=self.controls_overlay.is_manual_mode,
                              color_active=self.color_picker.is_active,
                              recording_armed=self._recording_is_armed(),
                              right_inset=column["content_right_inset"])

            # Color picker panel draws on top of the regular HUD elements (it
            # dims the 3D view behind it, same visual language as the Help
            # screen) but still below the controls overlay, consistent with
            # Help also losing to a loading overlay if both somehow overlap.
            self.color_picker.render(self.wnd.size)

            # Controls/loading overlay draws last of all, on top of every
            # other UI element -- while it's showing, it's meant to be the
            # thing you're looking at (it's explaining what the other UI
            # pieces do), so it should never be obscured by them.
            self.controls_overlay.update(self.world.stats())
            self.controls_overlay.render(self.wnd.size)
            self._render_recording_status_message(self.wnd.size)
            overlay_ms = (time.perf_counter() - t0) * 1000.0

        total_ms = (time.perf_counter() - frame_start) * 1000.0

        # Spike detection: track a short rolling average of frame times, and
        # if a frame comes in notably above that average, print a one-line
        # breakdown of where the time went. This is the diagnostic for
        # tracking down any remaining stutter -- rather than guess at
        # causes, the next time a stutter happens this will print exactly
        # which section (chunk streaming, mesh draw, or overlay draw) was
        # responsible, plus chunk-loading stats at that moment.
        self._frame_time_history.append(total_ms)
        if len(self._frame_time_history) > 30:
            self._frame_time_history.pop(0)
        rolling_avg = sum(self._frame_time_history) / len(self._frame_time_history)

        if len(self._frame_time_history) >= 10 and total_ms > max(rolling_avg * 3, 25.0):
            stats = self.world.stats()
            gpu_draw_text = self._format_optional_ms(self._last_gpu_draw_ms)
            other_ms = max(
                0.0,
                total_ms
                - input_ms
                - streaming_ms
                - scene_setup_ms
                - mesh_draw_ms
                - recording_read_ms
                - overlay_ms,
            )
            _LOG.warning(f"FRAME SPIKE: {total_ms:.1f}ms (avg {rolling_avg:.1f}ms) | "
                         f"input={input_ms:.1f}ms streaming={streaming_ms:.1f}ms "
                         f"scene_setup={scene_setup_ms:.1f}ms mesh_draw={mesh_draw_ms:.1f}ms "
                         f"mesh_cull={mesh_cull_ms:.1f}ms "
                         f"mesh_submit={mesh_submit_ms:.1f}ms "
                         f"gpu_query_wait={mesh_gpu_query_wait_ms:.1f}ms "
                         f"gpu_draw={gpu_draw_text} "
                         f"recording_read={recording_read_ms:.1f}ms "
                         f"recording_stage={recording_stage_ms:.1f}ms "
                         f"recording_drain={recording_drain_ms:.1f}ms "
                         f"overlay={overlay_ms:.1f}ms other={other_ms:.1f}ms | "
                         f"drawn={_chunks_drawn}/{len(self._chunk_gpu_objects)} "
                         f"loaded={stats['loaded']} pending={stats['pending']} "
                         f"ready={stats.get('ready', 0)} "
                         f"unload_pending={stats.get('unload_pending', 0)} "
                         f"wanted={stats.get('wanted', 0)}")
            _LOG.warning(
                "FRAME SPIKE STREAMING DETAIL: %s",
                self._format_streaming_frame_timing(streaming_timing),
            )

        self._frame_active_time_s += (total_ms / 1000.0)
        self._frame_count += 1
        now = time.time()
        if now - self._last_fps_print > 2.0:
            wall_interval_s = max(now - self._last_fps_print, 1e-6)
            active_interval_s = max(self._frame_active_time_s, 1e-6)
            rendered_fps = self._frame_count / active_interval_s
            wall_fps = self._frame_count / wall_interval_s
            if _LOG.isEnabledFor(logging.DEBUG):
                stats = self.world.stats()
                gpu_draw_text = self._format_optional_ms(self._last_gpu_draw_ms)
                _LOG.debug(f"rendered_fps={rendered_fps:.1f} wall_fps={wall_fps:.1f} "
                           f"frame_cost={rolling_avg:.1f}ms "
                           f"| chunks loaded={stats['loaded']} "
                           f"pending={stats['pending']} "
                           f"unload_pending={stats.get('unload_pending', 0)} "
                           f"drawn={_chunks_drawn}/{len(self._chunk_gpu_objects)} "
                           f"| speed={self.camera.move_speed:.1f}m/s "
                           f"| mesh_cull={mesh_cull_ms:.1f}ms "
                           f"mesh_submit={mesh_submit_ms:.1f}ms "
                           f"gpu_query_wait={mesh_gpu_query_wait_ms:.1f}ms "
                           f"recording_read={recording_read_ms:.1f}ms "
                           f"recording_stage={recording_stage_ms:.1f}ms "
                           f"recording_drain={recording_drain_ms:.1f}ms "
                           f"gpu_draw={gpu_draw_text}")
            self._frame_count = 0
            self._frame_active_time_s = 0.0
            self._last_fps_print = now

    render = on_render  # back-compat alias for older moderngl-window releases

    def _resolve_key(self, keys, *candidate_names):
        """
        Different moderngl-window/pyglet versions have used different names
        for the same key (e.g. LEFT_CONTROL vs LEFT_CTRL). Rather than hard-
        code one name and risk another AttributeError crash on a different
        installed version, try each known alias in turn and cache whichever
        one actually exists on this version's Keys class.
        """
        cache = getattr(self, "_key_resolve_cache", None)
        if cache is None:
            cache = {}
            self._key_resolve_cache = cache
        cache_key = candidate_names
        if cache_key in cache:
            return cache[cache_key]
        for name in candidate_names:
            if hasattr(keys, name):
                value = getattr(keys, name)
                cache[cache_key] = value
                return value
        raise AttributeError(
            f"None of the key names {candidate_names} exist on this "
            f"moderngl-window version's Keys class. Available attributes: "
            f"{[a for a in dir(keys) if not a.startswith('_')]}"
        )

    def _install_backend_modifier_probe(self) -> None:
        """Capture raw backend modifier bitmasks before they are reduced to shift/ctrl/alt."""
        handler = getattr(self.wnd, "_handle_modifiers", None)
        if not callable(handler):
            return

        def wrapped_handle_modifiers(mods):
            try:
                self._last_raw_modifiers = int(mods)
            except Exception:
                self._last_raw_modifiers = 0
            return handler(mods)

        self.wnd._handle_modifiers = wrapped_handle_modifiers

    def _raw_command_modifier_down(self) -> bool:
        raw_mods = int(getattr(self, "_last_raw_modifiers", 0) or 0)
        if raw_mods == 0:
            return False

        backend_module = type(self.wnd).__module__.lower()

        # pyglet: MOD_COMMAND is bit 6 (1 << 6)
        if ".pyglet." in backend_module:
            return (raw_mods & (1 << 6)) != 0

        # glfw: MOD_SUPER is bit 3 (1 << 3)
        if ".glfw." in backend_module:
            return (raw_mods & (1 << 3)) != 0

        # sdl2/pygame2: GUI modifiers are typically these bits.
        if ".sdl2." in backend_module or ".pygame2." in backend_module:
            return (raw_mods & 0x0C00) != 0

        return False

    def _key_is_down(self, keys, *candidate_names) -> bool:
        """Return True if any candidate key exists on this backend and is currently held."""
        for name in candidate_names:
            if hasattr(keys, name):
                if getattr(keys, name) in self._keys_down:
                    return True
        return False

    def _resolve_key_optional(self, keys, *candidate_names):
        """Return key code if present on this backend, else None."""
        for name in candidate_names:
            if hasattr(keys, name):
                return getattr(keys, name)
        return None

    def _digit_for_key(self, keys, key) -> int | None:
        """Return bookmark slot (1..9) for a key press across backend key name variants."""
        for digit in range(1, 10):
            candidates = (
                f"_{digit}",
                f"KEY_{digit}",
                f"NUMBER_{digit}",
                f"NUM_{digit}",
                f"NUMPAD_{digit}",
            )
            for name in candidates:
                if hasattr(keys, name) and getattr(keys, name) == key:
                    return digit

        # Common fallback for top-row number keys on many backends.
        if isinstance(key, int) and ord("1") <= key <= ord("9"):
            return key - ord("0")

        return None

    def _is_zero_key(self, keys, key) -> bool:
        """Check if the key is the 0 key across backend key name variants."""
        candidates = (
            "_0",
            "KEY_0",
            "NUMBER_0",
            "NUM_0",
            "NUMPAD_0",
        )
        for name in candidates:
            if hasattr(keys, name) and getattr(keys, name) == key:
                return True

        # Common fallback for top-row 0 key on many backends.
        if isinstance(key, int) and key == ord("0"):
            return True

        return False

    def _command_is_down(self, modifiers: KeyModifiers) -> bool:
        keys = self.wnd.keys

        # Raw backend modifiers are the most reliable source on macOS-style
        # command-key backends.
        if (
            self._active_platform_adapter().command_modifier_uses_control_fallback()
            and self._raw_command_modifier_down()
        ):
            return True

        # Prefer explicit modifier flags if available.
        for attr in ("super", "command", "logo", "meta"):
            if hasattr(modifiers, attr):
                try:
                    if bool(getattr(modifiers, attr)):
                        return True
                except Exception:
                    pass

        # Some macOS backends report Command through control-style flags.
        if self._active_platform_adapter().command_modifier_uses_control_fallback():
            for attr in ("ctrl", "control"):
                if hasattr(modifiers, attr):
                    try:
                        if bool(getattr(modifiers, attr)):
                            return True
                    except Exception:
                        pass

        # Fallback to key-state checks across backend naming variants.
        return self._key_is_down(
            keys,
            "LEFT_SUPER", "RIGHT_SUPER",
            "LEFT_COMMAND", "RIGHT_COMMAND",
            "COMMAND", "LCOMMAND", "RCOMMAND", "CMD",
            "LSUPER", "RSUPER", "LGUI", "RGUI",
            "LEFT_WINDOWS", "RIGHT_WINDOWS", "LWIN", "RWIN",
        )

    def _control_is_down(self, modifiers: KeyModifiers) -> bool:
        """Check if Control/Ctrl modifier key is currently down."""
        keys = self.wnd.keys
        # Prefer explicit modifier flags if available.
        for attr in ("ctrl", "control"):
            if hasattr(modifiers, attr):
                try:
                    if bool(getattr(modifiers, attr)):
                        return True
                except Exception:
                    pass
        # Fallback to key-state checks.
        return self._key_is_down(
            keys,
            "LEFT_CONTROL", "RIGHT_CONTROL",
            "LCTRL", "RCTRL", "CONTROL", "LCONTROL", "RCONTROL",
        )

    def _shift_is_down(self, modifiers: KeyModifiers) -> bool:
        """Check if Shift modifier key is currently down."""
        keys = self.wnd.keys
        if hasattr(modifiers, "shift"):
            try:
                if bool(getattr(modifiers, "shift")):
                    return True
            except Exception:
                pass
        return self._key_is_down(keys, "LEFT_SHIFT", "RIGHT_SHIFT", "LSHIFT", "RSHIFT", "SHIFT")

    def _bookmark_save_modifier_is_down(self, modifiers: KeyModifiers) -> bool:
        """Check if the platform-specific bookmark save modifier is down."""
        save_modifier = self._active_platform_adapter().bookmark_save_modifier()
        if save_modifier == "command":
            return self._command_is_down(modifiers)
        elif save_modifier == "control":
            return self._control_is_down(modifiers)
        return False

    def _load_bookmarks(self) -> None:
        self._bookmarks = viewer_bookmarks.load_bookmarks(
            self._bookmarks_path,
            logger=_LOG,
        )

    def _save_bookmarks(self) -> None:
        viewer_bookmarks.save_bookmarks(
            self._bookmarks_path,
            self._bookmarks,
            logger=_LOG,
        )

    def _save_bookmark_slot(self, slot: int) -> None:
        if not self._has_map_loaded:
            return
        self._bookmarks[slot] = viewer_bookmarks.bookmark_from_camera(
            self.camera.position,
            yaw=self.camera.yaw,
            pitch=self.camera.pitch,
        )
        self._save_bookmarks()
        _LOG.info(f"Saved camera bookmark {slot}.")

    def _recall_bookmark_slot(self, slot: int) -> bool:
        if not self._has_map_loaded:
            return False
        data = self._bookmarks.get(slot)
        if not data:
            _LOG.info(f"Bookmark {slot} is empty.")
            return False

        pos = data["position"]
        self.camera.position = np.array([float(pos[0]), float(pos[1]), float(pos[2])], dtype=np.float64)
        if not self._navigation_position_is_allowed(self.camera.position):
            safe_position = self._nearest_navigation_guard_position(self.camera.position)
            if safe_position is not None:
                self.camera.position = safe_position
                _LOG.info(f"Bookmark {slot} was outside the navigable map area; moved to nearest cave chunk.")
        self.camera.yaw = float(data["yaw"])
        pitch = float(data["pitch"])
        pitch_limit = getattr(self.camera, "_pitch_limit", None)
        if pitch_limit is not None:
            pitch = max(-float(pitch_limit), min(float(pitch_limit), pitch))
        self.camera.pitch = pitch
        self.camera.roll = 0.0  # Reset roll when loading a bookmark

        self.controls_overlay.show_panel()
        _LOG.info(f"Recalled camera bookmark {slot}.")
        return True

    def _delete_bookmark_slot(self, slot: int) -> None:
        if not self._has_map_loaded:
            return
        if slot not in self._bookmarks:
            _LOG.info(f"Bookmark {slot} does not exist; nothing to delete.")
            return
        del self._bookmarks[slot]
        self._save_bookmarks()
        _LOG.info(f"Deleted camera bookmark {slot}.")

    def _handle_bookmark_hotkey(self, key, modifiers: KeyModifiers) -> bool:
        if not self._has_map_loaded:
            return False
        keys = self.wnd.keys
        slot = self._digit_for_key(keys, key)
        if slot is None:
            return False

        # Platform-specific bookmark save modifier (Command on macOS, Control on Windows/Linux).
        # Shift+digit is accepted as a fallback on macOS for backends that don't report Command.
        save_modifier_down = self._bookmark_save_modifier_is_down(modifiers)
        shift_down = self._key_is_down(keys, "LEFT_SHIFT", "RIGHT_SHIFT", "LSHIFT", "RSHIFT")
        ctrl_down = self._control_is_down(modifiers)
        backspace_down = self._key_is_down(
            keys,
            "DELETE", "DEL",
            "FORWARD_DELETE", "FWDDELETE",
        )

        action = viewer_bookmarks.bookmark_hotkey_action(
            slot,
            save_modifier_down=save_modifier_down,
            shift_down=shift_down,
            ctrl_down=ctrl_down,
            backspace_down=backspace_down,
            shift_digit_save_fallback=(
                self._active_platform_adapter()
                .shift_digit_bookmark_save_fallback()
            ),
        )
        if action is viewer_bookmarks.BookmarkHotkeyAction.NONE:
            return False
        if action is viewer_bookmarks.BookmarkHotkeyAction.DELETE:
            self._delete_bookmark_slot(slot)
            return True
        if action is viewer_bookmarks.BookmarkHotkeyAction.SAVE:
            self._save_bookmark_slot(slot)
            return True
        self._recall_bookmark_slot(slot)
        return True

    def _option_look_active(self) -> bool:
        if not self._active_platform_adapter().option_left_mouse_look_enabled():
            return False
        return self._key_is_down(
            self.wnd.keys,
            "LEFT_ALT", "RIGHT_ALT", "LEFT_OPTION", "RIGHT_OPTION", "LALT", "RALT",
        )

    def _handle_continuous_input(self, dt: float):
        keys = self.wnd.keys
        forward_amt = 0.0
        right_amt = 0.0
        up_amt = 0.0
        if keys.W in self._keys_down:
            forward_amt += 1.0
        if keys.S in self._keys_down:
            forward_amt -= 1.0
        if keys.D in self._keys_down:
            right_amt += 1.0
        if keys.A in self._keys_down:
            right_amt -= 1.0
        e_key = self._resolve_key(keys, "E")
        q_key = self._resolve_key(keys, "Q")
        if e_key in self._keys_down:
            up_amt += 1.0
        if q_key in self._keys_down:
            up_amt -= 1.0

        shift_key = self._resolve_key(keys, "LEFT_SHIFT", "LSHIFT")
        speed_mult = 3.0 if shift_key in self._keys_down else 1.0
        if forward_amt or right_amt or up_amt:
            self._move_camera_guarded(forward_amt, right_amt, up_amt, dt, speed_mult)

        # Keyboard look fallback: arrow keys and I/J/K/L.
        # left/right or J/L = yaw, up/down or I/K = pitch.
        left_key = self._resolve_key(keys, "LEFT", "ARROW_LEFT")
        right_key = self._resolve_key(keys, "RIGHT", "ARROW_RIGHT")
        up_key = self._resolve_key(keys, "UP", "ARROW_UP")
        down_key = self._resolve_key(keys, "DOWN", "ARROW_DOWN")
        i_key = self._resolve_key_optional(keys, "I")
        j_key = self._resolve_key_optional(keys, "J")
        k_key = self._resolve_key_optional(keys, "K")
        l_key = self._resolve_key_optional(keys, "L")

        yaw_dir = 0.0
        if left_key in self._keys_down or (j_key is not None and j_key in self._keys_down):
            yaw_dir -= 1.0
        if right_key in self._keys_down or (l_key is not None and l_key in self._keys_down):
            yaw_dir += 1.0

        pitch_dir = 0.0
        if up_key in self._keys_down or (i_key is not None and i_key in self._keys_down):
            pitch_dir -= 1.0  # up arrow = look up
        if down_key in self._keys_down or (k_key is not None and k_key in self._keys_down):
            pitch_dir += 1.0  # down arrow = look down

        if yaw_dir or pitch_dir:
            look_amount = self._KEY_LOOK_PIXELS_PER_SECOND * dt
            self.camera.look(yaw_dir * look_amount, pitch_dir * look_amount)

        # Barrel roll: Z = counterclockwise (positive roll), X = clockwise (negative roll)
        z_key = self._resolve_key_optional(keys, "Z")
        x_key = self._resolve_key_optional(keys, "X")
        roll_dir = 0.0
        if z_key is not None and z_key in self._keys_down:
            roll_dir += 1.0
        if x_key is not None and x_key in self._keys_down:
            roll_dir -= 1.0
        if roll_dir:
            roll_speed = 2.0  # radians per second
            self.camera.barrel_roll(roll_dir * roll_speed * dt)

    def on_key_event(self, key, action, modifiers: KeyModifiers):
        # Cocoa may dispatch key callbacks before viewer controls exist or
        # after teardown has started. Input is not actionable in either state.
        if (
            not getattr(self, "_window_setup_complete", False)
            or getattr(self, "_closing_requested", False)
        ):
            return

        if self.controls_overlay is None:
            return
        keys = self.wnd.keys
        if action == keys.ACTION_PRESS:
            if self._handle_window_shortcut(key, modifiers):
                return
            if self.controls_overlay.is_waiting_for_begin:
                space_key = self._resolve_key_optional(keys, "SPACE", "SPACEBAR")
                if (
                    space_key is not None
                    and key == space_key
                    and self.controls_overlay.is_ready_to_begin
                ):
                    self.controls_overlay.dismiss_begin_screen()
                return
            if self._handle_bookmark_hotkey(key, modifiers):
                return
            if self._handle_recording_hotkey(key, modifiers):
                return
            if self._handle_reset_view_shortcut(key, modifiers):
                return
            self._keys_down.add(key)
        elif action == keys.ACTION_RELEASE:
            self._keys_down.discard(key)

    key_event = on_key_event

    def _handle_window_shortcut(self, key, modifiers: KeyModifiers) -> bool:
        """Handle desktop-standard window and open shortcuts."""
        shortcut_down = (
            self._command_is_down(modifiers)
            if self._active_platform_adapter().tk_primary_modifier_name() == "Command"
            else self._control_is_down(modifiers)
        )
        if not shortcut_down:
            return False

        close_key = self._resolve_key_optional(self.wnd.keys, "W")
        if close_key is not None and key == close_key:
            self.on_close()
            return True

        pause_key = self._resolve_key_optional(self.wnd.keys, "P")
        if (
            pause_key is not None
            and key == pause_key
            and self._shift_is_down(modifiers)
        ):
            if self._import_active:
                self._request_import_pause()
                return True

        open_key = self._resolve_key_optional(self.wnd.keys, "O")
        if open_key is not None and key == open_key:
            if self._has_map_loaded and not self._import_active:
                self._handle_open_button_click()
            return True

        return False

    def _request_import_pause(self) -> None:
        self._ensure_import_controller().request_pause()

    def _handle_recording_hotkey(self, key, modifiers: KeyModifiers) -> bool:
        """Use Shift+R to cancel countdown or stop active recording."""
        if not self._has_map_loaded or not self._recording_is_armed():
            return False
        record_key = self._resolve_key_optional(self.wnd.keys, "R")
        if record_key is None or key != record_key:
            return False
        if not self._shift_is_down(modifiers):
            return False
        self._toggle_recording()
        return True

    def _handle_reset_view_shortcut(self, key, modifiers: KeyModifiers) -> bool:
        """Handle CMD+0 (macOS) or CTRL+0 (Windows/Linux) to reset view."""
        keys = self.wnd.keys
        
        # Check if this is the 0 key
        if not self._is_zero_key(keys, key):
            return False
        
        shortcut_down = (
            self._command_is_down(modifiers)
            if self._active_platform_adapter().tk_primary_modifier_name() == "Command"
            else self._control_is_down(modifiers)
        )
        if shortcut_down:
            self.camera.reset_view()
            return True
        
        return False

    def _request_startup_focus_once(self) -> None:
        """Attempt to bring the app window to foreground once after startup."""
        if self._startup_focus_requested:
            return
        self._startup_focus_requested = True

        self._active_platform_adapter().focus_viewer_window(self.wnd)

    def _reset_transient_input_state(self, reason: str) -> None:
        """Clear transient input/capture flags that can get stuck across sleep/focus changes."""
        self._keys_down.clear()
        self._mouse_look_active = False
        self._mouse_look_left_option_active = False
        self._last_mouse_pos = None
        self.color_picker.on_mouse_release()
        if hasattr(self.wnd, "mouse_exclusivity"):
            self.wnd.mouse_exclusivity = False

        now = time.time()
        if now - self._last_input_reset_log > 3.0:
            _LOG.info(f"Input state reset ({reason}).")
            self._last_input_reset_log = now

    def _query_runtime_iconified_state(self) -> bool:
        """Best-effort minimized/backgrounded detection across window backends."""
        for target in (getattr(self.wnd, "_window", None), self.wnd):
            if target is None:
                continue
            for attr in ("minimized", "is_minimized", "iconified"):
                try:
                    if hasattr(target, attr) and bool(getattr(target, attr)):
                        return True
                except Exception:
                    pass
            for attr in ("visible", "is_visible"):
                try:
                    if hasattr(target, attr):
                        value = getattr(target, attr)
                        value = value() if callable(value) else value
                        if value is False:
                            return True
                except Exception:
                    pass
        return False

    def _set_background_pause(self, should_pause: bool, reason: str) -> None:
        self._is_iconified = bool(should_pause)
        if self._is_background_paused == self._is_iconified:
            return

        self._is_background_paused = self._is_iconified
        if self._is_background_paused:
            self._reset_transient_input_state(reason)
            if self._has_map_loaded and hasattr(self, "world"):
                self.world.pause()
        else:
            if self._has_map_loaded and hasattr(self, "world"):
                self.world.resume()

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

    def _handle_mouse_look_motion(self, x, y, dx, dy):
        # Cocoa can deliver passive mouse-move callbacks while the native
        # window exists but before our Python-side controls are fully built.
        # Treat those early/late events as no-ops so ctypes does not print
        # ignored callback exceptions to stderr.
        if (
            not getattr(self, "_window_setup_complete", False)
            or getattr(self, "_closing_requested", False)
        ):
            return

        # Color picker's RGB sliders still use continuous drag (a
        # separate feature from the brightness/render-distance controls
        # below, which were converted to discrete +/- steppers) -- this
        # still needs to take priority over camera look while one of its
        # sliders is being dragged, same reasoning as before.
        color_picker = getattr(self, "color_picker", None)
        if color_picker is not None and color_picker.is_dragging:
            color_picker.on_mouse_drag(x, y, self.wnd.size)
            return
        # macOS-friendly fallback: Option + pointer movement can drive
        # look even without a physical click/drag gesture.
        if self._option_look_active() or self._mouse_look_active:
            # On the first event after mouse exclusivity is enabled the
            # backend warps the cursor to the window centre, generating a
            # large spurious delta.  _last_mouse_pos being None is the
            # sentinel for "just activated": absorb that one event and
            # record a real position so subsequent deltas are applied.
            if self._last_mouse_pos is None:
                self._last_mouse_pos = (x, y)
                return
            self._last_mouse_pos = (x, y)
            self.camera.look(dx, dy)

    def on_mouse_position_event(self, x, y, dx, dy):
        self._handle_mouse_look_motion(x, y, dx, dy)

    mouse_position_event = on_mouse_position_event

    def on_mouse_drag_event(self, x, y, dx, dy):
        if self.controls_overlay.is_waiting_for_begin:
            return
        self._handle_mouse_look_motion(x, y, dx, dy)

    mouse_drag_event = on_mouse_drag_event

    def on_mouse_press_event(self, x, y, button):
        if self.controls_overlay.is_waiting_for_begin:
            return
        if self.controls_overlay.is_manual_mode:
            self.controls_overlay.hide_help()
            return

        look_button_name = self._active_platform_adapter().mouse_look_button_name()
        look_button = self.wnd.mouse.left if look_button_name == "left" else self.wnd.mouse.right

        if self._recording_hides_hud():
            if button == self.wnd.mouse.left and self._option_look_active():
                self._mouse_look_active = True
                self._mouse_look_left_option_active = True
                self._last_mouse_pos = None
                self.wnd.mouse_exclusivity = True
                return
            if button == look_button:
                self._mouse_look_active = True
                self._last_mouse_pos = None
                self.wnd.mouse_exclusivity = True
            return

        if button == self.wnd.mouse.left:
            # macOS-friendly mouse-look: Option + left-drag avoids relying
            # on right-click behavior (which can vary across trackpads/mice).
            if self._option_look_active():
                self._mouse_look_active = True
                self._mouse_look_left_option_active = True
                self._last_mouse_pos = None
                self.wnd.mouse_exclusivity = True
                return

            # Check order: all three steppers, then mesh/texture toggle
            # buttons, then minimap. All four pieces (brightness, global
            # light, render distance, button block) now live together in
            # the same bottom-right column -- check order only matters in
            # the sense that each needs to happen before falling through
            # to the next, since their hit areas don't overlap.
            column = self._right_column_layout(self.wnd.size)
            brightness_anchor_x, brightness_anchor_y = column["brightness_anchor"]
            ambient_anchor_x, ambient_anchor_y = column["ambient_anchor"]
            render_distance_anchor_x, render_distance_anchor_y = column["render_distance_anchor"]
            buttons_top_y = column["buttons_top_y"]

            # While map-loading overlays are active (startup fullscreen or
            # teleport panel), keep the right-side button block inert.
            # Manual HELP mode is intentionally excluded so the same
            # buttons remain usable when the user explicitly opens help.
            buttons_locked_for_loading = self._buttons_locked_for_loading()

            if self.light_stepper.on_mouse_press(x, y, brightness_anchor_x, brightness_anchor_y):
                return

            if self.ambient_stepper.on_mouse_press(x, y, ambient_anchor_x, ambient_anchor_y):
                return

            if self.render_distance_stepper.on_mouse_press(x, y, render_distance_anchor_x, render_distance_anchor_y):
                return

            if self.render_mode_buttons.hit_test_record(
                x, y, self.wnd.size, buttons_top_y, column["content_right_inset"]
            ):
                self._toggle_recording()
                return

            if buttons_locked_for_loading:
                if (
                    self.render_mode_buttons.hit_test_mesh(x, y, self.wnd.size, buttons_top_y, column["content_right_inset"])
                    or self.render_mode_buttons.hit_test_texture(x, y, self.wnd.size, buttons_top_y, column["content_right_inset"])
                    or self.render_mode_buttons.hit_test_shade(x, y, self.wnd.size, buttons_top_y, column["content_right_inset"])
                    or self.render_mode_buttons.hit_test_help(x, y, self.wnd.size, buttons_top_y, column["content_right_inset"])
                    or self.render_mode_buttons.hit_test_color(x, y, self.wnd.size, buttons_top_y, column["content_right_inset"])
                    or self.render_mode_buttons.hit_test_open(x, y, self.wnd.size, buttons_top_y, column["content_right_inset"])
                ):
                    return

            clicked_button = self.render_mode_buttons.on_mouse_press(
                x, y, self.wnd.size, buttons_top_y, column["content_right_inset"]
            )
            if clicked_button == "shade":
                self._apply_shading_toggle()
                return
            elif clicked_button == "help":
                # Toggle: if the help screen is already showing (manual
                # mode), a second click closes it; otherwise show it.
                # Showing help intentionally overrides whatever loading
                # overlay might currently be active (e.g. a brief teleport
                # panel) -- an explicit click is a clear request to see
                # the controls right now, which should win over a
                # transient loading indicator.
                if self.controls_overlay.is_manual_mode:
                    self.controls_overlay.hide_help()
                else:
                    self.controls_overlay.show_help()
                return
            elif clicked_button == "color":
                if self.color_picker.is_active:
                    self.color_picker.hide()
                else:
                    self.color_picker.show()
                return
            elif clicked_button == "open":
                self._handle_open_button_click()
                return
            elif clicked_button == "record":
                self._toggle_recording()
                return
            elif clicked_button is not None:
                # "mesh" or "texture" -- already toggled internally by
                # render_mode_buttons.on_mouse_press, nothing further needed here.
                return

            # While the color picker panel is open, it behaves like a
            # modal -- clicks inside the panel interact with its sliders.
            # A click outside closes the picker and is consumed so that
            # dismissing it cannot also trigger unrelated world/UI actions
            # underneath on the same click.
            if self.color_picker.is_active:
                if self.color_picker.hit_test_panel(x, y, self.wnd.size):
                    self.color_picker.on_mouse_press(x, y, self.wnd.size)
                else:
                    self.color_picker.hide()
                return

            minimap_target = None
            if self._has_map_loaded and self.minimap is not None:
                minimap_target = self.minimap.world_xz_for_click(x, y, self.wnd.size)
            if minimap_target is not None:
                target_x, target_z = minimap_target
                # Land at an actual occupied height near that X/Z, rather
                # than blindly keeping the camera's previous Y -- a click
                # on the (top-down, height-blind) minimap doesn't tell us
                # which vertical level was meant, so we look up real chunk
                # bounds at that column and pick whichever level is
                # closest to the camera's current height (see
                # find_landing_position in caveviewer.core.chunking.metadata).
                # This is what prevents landing above or below the actual
                # passage.
                old_x = float(self.camera.position[0])
                old_z = float(self.camera.position[2])
                landing_x, landing_y, landing_z = chunker.find_landing_position(
                    self.manifest, target_x, target_z,
                    preferred_y=float(self.camera.position[1]),
                )
                self.camera.position[0] = landing_x
                self.camera.position[1] = landing_y
                self.camera.position[2] = landing_z

                # Reorient toward the teleport direction so the camera looks
                # into the new area rather than potentially facing blank space.
                # Only rotate when the click is far enough away to give a
                # meaningful direction (>0.5 m threshold avoids jitter for
                # near-by clicks that don't imply a clear travel direction).
                dx = landing_x - old_x
                dz = landing_z - old_z
                if math.hypot(dx, dz) > 0.5:
                    self.camera.yaw   = math.atan2(dz, dx)
                    self.camera.pitch = 0.0
                    self.camera.roll  = 0.0

                # Show the controls panel briefly while the newly-teleported
                # area's chunks stream in around the camera -- same content
                # as the full-screen startup overlay, just smaller since
                # teleporting is quick and shouldn't block the whole view.
                self.controls_overlay.show_panel()
                return

            # On Windows/Linux, left-click that doesn't hit any UI activates mouse look
            if look_button_name == "left":
                self._mouse_look_active = True
                self._last_mouse_pos = None
                self.wnd.mouse_exclusivity = True
            return
        if button == look_button and look_button_name == "right":
            self._mouse_look_active = True
            self._last_mouse_pos = None
            self.wnd.mouse_exclusivity = True

    mouse_press_event = on_mouse_press_event

    def on_mouse_release_event(self, x, y, button):
        look_button_name = self._active_platform_adapter().mouse_look_button_name()
        look_button = self.wnd.mouse.left if look_button_name == "left" else self.wnd.mouse.right

        if button == self.wnd.mouse.left:
            if self._mouse_look_left_option_active:
                self._mouse_look_left_option_active = False
                self._mouse_look_active = False
                self.wnd.mouse_exclusivity = False
                return
            # On Windows/Linux, left-click release ends mouse look
            if self._mouse_look_active and look_button_name == "left":
                self._mouse_look_active = False
                self.wnd.mouse_exclusivity = False
                return
            self.color_picker.on_mouse_release()
            return
        if button == look_button and look_button_name == "right":
            self._mouse_look_active = False
            self.wnd.mouse_exclusivity = False

    mouse_release_event = on_mouse_release_event

    def on_mouse_scroll_event(self, x_offset, y_offset):
        self.camera.adjust_speed(y_offset)

    mouse_scroll_event = on_mouse_scroll_event

    def _cancel_active_import(self) -> None:
        self._ensure_import_controller().cancel_active_import()

    def _shutdown_active_import(self) -> None:
        self._ensure_import_controller().shutdown()

    def on_close(self):
        if self._closing_requested:
            return
        self._closing_requested = True

        if hasattr(self, "wnd"):
            try:
                self.wnd.mouse_exclusivity = False
            except Exception:
                pass

        if getattr(self, "_import_active", False):
            self._shutdown_active_import()

        if self._has_map_loaded:
            self._teardown_current_map(final_shutdown=True)
        self._release_window_resources()

        # Ensure the backend window loop receives an explicit close request.
        if hasattr(self, "wnd") and hasattr(self.wnd, "close"):
            try:
                self.wnd.close()
            except Exception:
                pass

    close = on_close


def _run_moderngl_window_config(config_class: type, args=None) -> None:
    """
    Run moderngl-window while preserving CaveViewer's normal shutdown path
    when the blocking render loop is interrupted by Ctrl+C/SIGINT.

    moderngl-window destroys the backend window only after its loop exits
    normally.  A KeyboardInterrupt can arrive inside any render callback and
    bypass that tail cleanup, so create the config explicitly and close/destroy
    the window ourselves before re-raising to the application boundary.
    """
    config = mglw.create_window_config_instance(config_class, args=args)
    window_destroyed_by_runner = False
    try:
        mglw.run_window_config_instance(config)
        window_destroyed_by_runner = True
    except BaseException:
        wnd = getattr(config, "wnd", None)
        if wnd is not None:
            try:
                if not getattr(wnd, "is_closing", False):
                    wnd.close()
            except Exception:
                _LOG.exception("Error while closing viewer after interrupted window loop.")
        raise
    finally:
        if not window_destroyed_by_runner:
            wnd = getattr(config, "wnd", None)
            if wnd is not None:
                try:
                    wnd.destroy()
                except Exception:
                    pass


def _launch_viewer_window() -> None:
    """Launch with dimensions expressed in the selected backend's coordinates."""
    if get_platform_adapter().viewer_uses_glfw_native_initial_size():
        # Linux GLFW sizing happens after the Wayland/X11 backend is selected,
        # using that backend's DPI-aware work-area coordinate system.
        CaveViewerWindow.window_size = _DEFAULT_WINDOW_SIZE
    else:
        CaveViewerWindow.window_size = _desktop_relative_window_size()
    run_window_config(
        CaveViewerWindow,
        runner=_run_moderngl_window_config,
        window_size_fraction=_DESKTOP_WINDOW_SCALE,
        fallback_window_size=_DEFAULT_WINDOW_SIZE,
        force_resizable_window=True,
    )


def run_viewer(cache_dir: str, textures_dir: str):
    manifest = chunker.load_manifest(cache_dir)

    # Set as class attributes rather than passing through run_window_config's
    # kwargs -- see the comment on CaveViewerWindow's class attributes above
    # for why. This sidesteps moderngl-window version differences in how
    # (or whether) run_window_config forwards extra keyword arguments.
    CaveViewerWindow.cave_cache_dir = cache_dir
    CaveViewerWindow.cave_textures_dir = textures_dir
    CaveViewerWindow.cave_manifest = manifest

    _launch_viewer_window()


def run_viewer_with_pending_import(model_descriptor: dict, textures_dir: str):
    """
    Launches the viewer window for a map that needs FIRST-TIME import
    (no managed cache yet) -- used by caveviewer.app's main() instead
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
    CaveViewerWindow.cave_cache_dir = None
    CaveViewerWindow.cave_textures_dir = None
    CaveViewerWindow.cave_manifest = None
    CaveViewerWindow.cave_pending_import = {
        "model_descriptor": model_descriptor,
        "textures_dir": textures_dir,
    }

    try:
        _launch_viewer_window()
    except RuntimeError as e:
        # Suppress the known "no initial map" runtime error that can occur
        # when the viewer is launched without a preloaded map and the GUI
        # is closed; let other RuntimeErrors propagate.
        msg = str(e)
        if "Neither CaveViewerWindow.cave_cache_dir" in msg and "must be set" in msg:
            # Clean exit without a traceback
            _LOG.info("Viewer exited without a preloaded map.")
            return
        raise
