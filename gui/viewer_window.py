"""
gui/viewer_window.py

The actual OpenGL window: owns the moderngl context, the free-fly camera,
the StreamingWorld (which decides what to load/unload), and the per-chunk
GPU buffers/textures. This is where everything else in core/ and gui/
gets wired together into a runnable program.

Each loaded chunk becomes a small set of moderngl VAOs, one per material
group within that chunk (so each can be drawn with its own bound texture).
We keep a dict: cell -> list[(vao, texture_material_name)] so unload is a
simple lookup-and-release.
"""

from __future__ import annotations

import json
import os
import sys
import time

import numpy as np
import moderngl
import moderngl_window as mglw
from moderngl_window.context.base import KeyModifiers

from core import chunker
from core.logging_utils import get_logger
from core.streaming_world import StreamingWorld, StreamingConfig
from core.texture_manager import TextureManager
from gui.camera import FlyCamera
from gui.minimap import Minimap
from gui.render_mode_buttons import RenderModeButtons
from gui.controls_overlay import ControlsOverlay
from gui.stepper_control import StepperControl
from gui.color_picker import ColorPicker
from gui.import_progress_panel import ImportProgressPanel
from gui import bitmap_font
from gui.platform.factory import get_platform_adapter
from caveviewer_version import APP_NAME, APP_VERSION

_LOG = get_logger("CaveViewer")


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


def _resource_base_dir() -> str:
    """
    Returns the correct base directory to resolve bundled resources (like
    the shaders/ folder) from, whether running normally from source or
    packaged into a standalone executable via PyInstaller.

    When PyInstaller builds a frozen executable, bundled data files are
    extracted to a temporary directory at runtime, exposed via
    `sys._MEIPASS` -- NOT the directory containing this .py file (which,
    in a frozen build, doesn't really exist as a normal file on disk at
    all). Checking for `sys.frozen` is the standard way to detect this and
    branch accordingly; see build_exe.py / CaveViewer.spec for the matching
    PyInstaller config that actually places shaders/ at the right spot
    inside the bundle.
    """
    if getattr(sys, "frozen", False):
        return sys._MEIPASS  # type: ignore[attr-defined]
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


SHADER_DIR = os.path.join(_resource_base_dir(), "shaders")


def _runtime_app_icon_path() -> str:
    assets_dir = os.path.join(_resource_base_dir(), "gui", "assets")
    if sys.platform == "win32":
        filenames = ("app_icon_windows.png",)
    else:
        filenames = ("app_icon_macos.png",)
    for filename in filenames:
        path = os.path.join(assets_dir, filename)
        if os.path.exists(path):
            return path
    return os.path.join(assets_dir, filenames[0])


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
    # Start larger on desktop platforms so the HELP overlay and right-side
    # controls have comfortable vertical room on first launch.
    # Use a 16:10 baseline (more vertical space than 16:9) while keeping
    # aspect_ratio unlocked so manual resizing remains fully flexible.
    window_size = (1600, 1000) if sys.platform.startswith("linux") else (1440, 900)
    resizable = True
    vsync = True
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
    RIGHT_COLUMN_PANEL_SIDE_PAD = 14
    RIGHT_COLUMN_PANEL_TOP_PAD = 12
    RIGHT_COLUMN_PANEL_BOTTOM_PAD = 14
    RIGHT_COLUMN_PANEL_RIGHT_MARGIN = 20
    RIGHT_COLUMN_PANEL_BOTTOM_MARGIN = 20
    RIGHT_COLUMN_PANEL_LABEL_GAP = 10
    RIGHT_COLUMN_PANEL_LABEL_SIZE = 1.7
    RIGHT_COLUMN_PANEL_FILL_RGBA = (0.09, 0.12, 0.16, 0.84)
    RIGHT_COLUMN_PANEL_BORDER_RGBA = (0.42, 0.54, 0.72, 0.62)
    RIGHT_COLUMN_PANEL_BORDER_PX = 1.5

    # Startup focus forcing can make bundled macOS app windows appear in a
    # corner first and then jump as the window manager re-places them.
    # Default to disabled for frozen macOS builds; allow override.
    FORCE_STARTUP_FOCUS_ENV = "CAVEVIEWER_FORCE_STARTUP_FOCUS"

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._set_runtime_window_icon()

        force_focus_env = os.getenv(self.FORCE_STARTUP_FOCUS_ENV, "").strip().lower()
        force_focus = force_focus_env in {"1", "true", "yes", "on"}
        self._startup_focus_enabled = True
        if sys.platform == "darwin" and getattr(sys, "frozen", False) and not force_focus:
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

        have_ready_cache = CaveViewerWindow.cave_cache_dir is not None
        have_pending_import = CaveViewerWindow.cave_pending_import is not None

        if not have_ready_cache and not have_pending_import:
            raise RuntimeError(
                "Neither CaveViewerWindow.cave_cache_dir nor .cave_pending_import "
                "was set before launch. One or the other must be set by "
                "run_viewer() / run_viewer_with_pending_import() before "
                "constructing this window."
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

        self._keys_down = set()
        self._last_raw_modifiers = 0
        self._mouse_look_active = False
        self._mouse_look_left_option_active = False
        self._last_mouse_pos = None
        self._frame_count = 0
        self._last_fps_print = time.time()
        self._frame_active_time_s = 0.0
        self._frame_time_history: list[float] = []
        self._last_gpu_draw_ms = 0.0
        self._last_input_reset_log = 0.0
        self._layout_cache_size: tuple | None = None
        self._layout_cache_result: dict | None = None
        self._is_iconified = False
        self._is_background_paused = False
        self._closing_requested = False
        self._startup_focus_requested = False
        self._upload_chunks_per_frame = _env_int("CAVEVIEWER_UPLOAD_CHUNKS_PER_FRAME", 1, 1, 16)
        self._upload_time_budget_ms = _env_float("CAVEVIEWER_UPLOAD_TIME_BUDGET_MS", 3.0, 0.5, 50.0)
        self._platform_adapter = get_platform_adapter()
        self._bookmarks_path: str | None = None
        self._bookmarks: dict[int, dict] = {}

        self._install_backend_modifier_probe()

        # Headlamp brightness control: a -/value/+ stepper, right side of
        # the screen. Replaced a draggable vertical slider -- dragging the
        # handle was unreliable for at least one person testing this
        # (clicking the track worked, grabbing the handle to drag did
        # not), so this sidesteps the whole class of problem by using
        # discrete +/-1 clicks instead of continuous drag-tracking.
        # Range/default unchanged from the old slider (0-10, default 3).
        self.light_stepper = StepperControl(self.ctx, "BRIGHTNESS", initial_value=5, min_value=0, max_value=10)

        # Render distance control: a -/value/+ stepper, left side of the
        # screen, mirroring the brightness control's placement logic but
        # on the opposite side. Directly drives
        # self.world.config.load_radius_cells live, same as the slider it
        # replaced. Range is 1-10 chunk-radius units. Default is 3 for a
        # balanced initial view radius without being overly aggressive on
        # memory usage. StreamingWorld's max_loaded_chunks safety valve
        # (see core/streaming_world.py) still applies underneath this as
        # a hard backstop regardless of what this is set to.
        self.render_distance_stepper = StepperControl(
            self.ctx, "DISTANCE", initial_value=3, min_value=1, max_value=10
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
        self.ambient_stepper = StepperControl(self.ctx, "GLOBAL LIGHT", initial_value=5, min_value=0, max_value=10)

        # Mesh/Texture toggle buttons, stacked just below the brightness
        # slider. Mesh = wireframe overlay on/off; Texture = whether the
        # photo texture is sampled or the surface falls back to plain lit
        # gray. See gui/render_mode_buttons.py for the four resulting
        # combined display states.
        self.render_mode_buttons = RenderModeButtons(self.ctx, texture_enabled=True, wireframe_enabled=False,
                                                      smooth_shading_enabled=True)
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
        self.import_progress_panel = ImportProgressPanel(self.ctx)

        self.ctx.enable(moderngl.DEPTH_TEST)
        self.ctx.enable(moderngl.CULL_FACE)

        # Map-specific state (world, manifest, camera, minimap, texture
        # manager, chunk GPU objects) lives in its own method, separate
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
        # Per-chunk, per-material CPU-side data for instant SHADE toggle:
        # each entry holds (mat_name, positions, uvs, smooth_normals, flat_normals)
        # tuples in the same order as _chunk_gpu_objects, so toggling shading
        # can zip the two lists and rewrite each VBO in place via vbo.write().
        self._chunk_normal_cache: dict[tuple, list] = {}
        # Per-cell world-space AABBs for frustum culling, populated in
        # _load_map from the manifest's pre-computed bounding boxes.
        self._chunk_aabbs: dict[tuple, tuple] = {}
        self._has_map_loaded = False
        self._pending_import_started = False
        self._initial_chunks_loaded = False
        self._chunk_prep_progress = 0.0
        self._chunk_prep_complete_until = None
        self._chunk_prep_completion_armed = False
        self._window_resources_released = False

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

        self.texture_manager = TextureManager(self.ctx, self.textures_dir, self.manifest["mtl_materials"])
        self.texture_manager.validate_textures()

        def predecode_textures_for_chunk(chunk_data):
            # Called from a background worker thread (see StreamingWorld) --
            # decodes JPEGs for every material this chunk uses, ahead of
            # time, so the eventual main-thread GPU upload can use
            # already-decoded pixels rather than doing a slow
            # decode-and-upload combination.
            for mat_name in chunk_data.groups.keys():
                self.texture_manager.decode_for_material(mat_name)

        chunk_size = chunker.manifest_chunk_size(self.manifest)
        if chunk_size is None:
            raise ValueError(
                "Map cache manifest is missing a valid chunk_size. "
                "Rebuild the _cache folder with this version of CaveViewer."
            )
        configured_chunk_size = chunker.configured_chunk_size()
        _LOG.info(f"Opening map cache with manifest chunk size: {chunk_size:g}m.")
        if abs(chunk_size - configured_chunk_size) > 1e-6:
            _LOG.info(
                f"Current {chunker.CHUNK_SIZE_ENV_VAR} setting is {configured_chunk_size:g}m, "
                "but existing/prebuilt caches stream using the chunk size recorded in manifest.json."
            )
        config = StreamingConfig(
            chunk_size=chunk_size,
            load_radius_cells=self.render_distance_stepper.value,
            unload_radius_margin=1,
        )
        self.world = StreamingWorld(self.cache_dir, config, on_decode_textures=predecode_textures_for_chunk)

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

        # Build world-space AABB lookup for every cell in the manifest so
        # the frustum culler can skip chunks outside the view each frame.
        self._chunk_aabbs = {
            tuple(int(v) for v in cell_str.split("_")): (
                np.array(info["bounds_min"], dtype=np.float32),
                np.array(info["bounds_max"], dtype=np.float32),
            )
            for cell_str, info in manifest["chunks"].items()
        }

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

    def _teardown_current_map(self) -> None:
        """
        Cleanly releases everything specific to the CURRENTLY loaded map
        before _load_map() builds a new one -- stops StreamingWorld's
        background threads and waits for them to actually exit, then
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
        """
        if not self._has_map_loaded:
            return

        self.world.shutdown()

        for cell in list(self._chunk_gpu_objects.keys()):
            self._on_chunk_unload(cell)

        # belt-and-suspenders: if anything was somehow left behind (it
        # shouldn't be, given the loop above), don't carry it into the
        # next map's state
        self._chunk_gpu_objects.clear()
        self._chunk_normal_cache.clear()
        self._chunk_aabbs.clear()

        if hasattr(self, "texture_manager") and self.texture_manager is not None:
            self.texture_manager.shutdown()

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

        self._keys_down.clear()
        self._mouse_look_active = False
        self._mouse_look_left_option_active = False
        self._last_mouse_pos = None

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
        _release_attr(self, "_hud_panel_program")

        CaveViewerWindow.cave_cache_dir = None
        CaveViewerWindow.cave_textures_dir = None
        CaveViewerWindow.cave_manifest = None
        CaveViewerWindow.cave_pending_import = None

    def _present_import_progress_frame(self) -> None:
        if hasattr(self.wnd, "swap_buffers"):
            self.wnd.swap_buffers()
        else:
            self.ctx.finish()

    def load_new_map(self, cache_dir: str, textures_dir: str, manifest: dict) -> None:
        """
        Switches the viewer to a different map without closing the
        window -- called by the OPEN button's click handler once a new
        folder has been picked and imported/cached (see
        caveviewer.py's find_input_files/import_and_cache, reused as-is
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
        # Local imports here (not at module top) since these pull in
        # tkinter and the parser/chunker modules, which the rest of this
        # file doesn't otherwise need -- same reasoning caveviewer.py
        # already uses for its own local imports of these.
        from caveviewer import pick_folder_dialog, find_model_file, import_and_cache_any
        from core import chunker as chunker_module

        folder = pick_folder_dialog()
        if not folder:
            _LOG.info("Open cancelled -- no folder selected.")
            return

        folder = os.path.abspath(folder)
        _LOG.info(f"Opening new map from: {folder}")

        try:
            model_descriptor = find_model_file(folder)
        except FileNotFoundError as e:
            # Match caveviewer.py's startup behavior: allow selecting a
            # folder that already contains a built _cache,
            # or selecting the cache directory itself directly.
            prebuilt_cache = os.path.join(folder, chunker_module.CACHE_DIRNAME)
            legacy_prebuilt_cache = os.path.join(folder, chunker_module.LEGACY_CACHE_DIRNAME)
            textures_dir = folder
            if not os.path.exists(os.path.join(prebuilt_cache, chunker_module.MANIFEST_NAME)):
                if os.path.exists(os.path.join(legacy_prebuilt_cache, chunker_module.MANIFEST_NAME)):
                    _LOG.info(f"Found legacy cache in: {legacy_prebuilt_cache}")
                    prebuilt_cache = legacy_prebuilt_cache
                elif os.path.exists(os.path.join(folder, chunker_module.MANIFEST_NAME)):
                    _LOG.info(f"Found cache manifest in selected directory: {folder}")
                    prebuilt_cache = folder
                    textures_dir = folder
            if not os.path.exists(os.path.join(prebuilt_cache, chunker_module.MANIFEST_NAME)):
                _LOG.warning(f"Could not open this folder: {e}")
                return

            try:
                new_manifest = chunker_module.load_manifest(prebuilt_cache)
            except Exception as manifest_err:
                _LOG.error(f"Failed to load the selected prebuilt map: {manifest_err}")
                return

            map_name = os.path.basename(new_manifest.get("source_obj") or folder)
            _LOG.info(f"Switching to prebuilt map: {map_name}")
            _LOG.info(f"Using cache directory: {prebuilt_cache}")
            self.load_new_map(prebuilt_cache, textures_dir, new_manifest)
            _LOG.info(f"Now viewing: {map_name}")
            return

        source_path = model_descriptor.get("obj_path") or model_descriptor.get("glb_path")
        map_name = os.path.basename(source_path)

        # If there's no valid cache yet, this is the same one-time import
        # cost as opening any brand-new map for the first time -- show
        # the progress panel so it's visible what's happening rather than
        # the window appearing to freeze with no explanation.
        already_cached = chunker_module.cache_is_valid(source_path)

        def on_progress(stage: str, fraction: float):
            self.import_progress_panel.render(self.wnd.size, map_name, stage, fraction)
            # Explicitly push this frame to the screen -- the normal
            # render loop is paused while import_and_cache_any() runs
            # synchronously below, so without this, nothing drawn here
            # would actually become visible until the import finishes
            # and the next regular frame happens to render.
            #
            # swap_buffers() is moderngl-window's standard, long-standing
            # method for this and should be present on any version in
            # use -- but since this project has already hit real
            # cross-version API differences before (see _resolve_key,
            # and the render()/on_render() hook rename), this is wrapped
            # defensively rather than assumed: if swap_buffers truly
            # isn't there on some version, ctx.finish() at least forces
            # the GPU to complete the draw rather than crashing outright,
            # even though it can't guarantee the frame reaches the
            # screen without a real swap.
            self._present_import_progress_frame()

        try:
            if not already_cached:
                on_progress("starting import", 0.0)
                cache_dir = import_and_cache_any(model_descriptor, folder, force_rebuild=False,
                                                   extra_progress_cb=on_progress)
            else:
                on_progress("loading cached map", 1.0)
                cache_dir = chunker_module.get_cache_dir(source_path)
        except Exception as e:
            _LOG.error(f"Failed to import this map: {e}")
            return

        try:
            new_manifest = chunker_module.load_manifest(cache_dir)
        except Exception as e:
            _LOG.error(f"Failed to load the new map's manifest after import: {e}")
            return

        _LOG.info(f"Switching to: {map_name}")
        self.load_new_map(cache_dir, folder, new_manifest)
        _LOG.info(f"Now viewing: {map_name}")

    def _run_pending_import(self) -> None:
        """
        Runs the FIRST-TIME import for the map the program was launched
        with, when CaveViewerWindow.cave_pending_import was set instead
        of an already-built cache (see run_viewer_with_pending_import()
        at the bottom of this file, and main()'s use of it in
        caveviewer.py). Called once, from on_render()'s first frame --
        see the _has_map_loaded branch there for why it's deferred to
        that point rather than running before the window even opens.

        Format-agnostic: works the same regardless of whether the
        pending import is an .obj or .glb (see
        caveviewer.py's find_model_file()/import_and_cache_any(), which
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
        fmt = model_descriptor["format"]
        source_path = model_descriptor.get("obj_path") or model_descriptor.get("glb_path")
        map_name = os.path.basename(source_path)

        from caveviewer import import_and_cache_any
        from core import chunker as chunker_module

        already_cached = chunker_module.cache_is_valid(source_path)

        def on_progress(stage: str, fraction: float):
            self.import_progress_panel.render(self.wnd.size, map_name, stage, fraction)
            self._present_import_progress_frame()

        try:
            if not already_cached:
                on_progress("starting import", 0.0)
                cache_dir = import_and_cache_any(model_descriptor, textures_dir, force_rebuild=False,
                                                   extra_progress_cb=on_progress)
            else:
                on_progress("loading cached map", 1.0)
                cache_dir = chunker_module.get_cache_dir(source_path)

            new_manifest = chunker_module.load_manifest(cache_dir)
        except Exception as e:
            _LOG.error(f"Failed to import this map: {e}")
            _LOG.error("Closing -- there's no map to show without a successful import.")
            # wnd.close() is moderngl-window's standard way to request a
            # clean shutdown, but -- same reasoning as the swap_buffers
            # defensive check above -- this project has hit real cross-
            # version API differences before, so this is wrapped rather
            # than assumed. Worst case if .close() isn't present: the
            # window stays open showing the neutral background from the
            # on_render() guard above (since _has_map_loaded stays False
            # and the import won't be retried), rather than crashing
            # inside this already-error-handling block.
            if hasattr(self.wnd, "close"):
                self.wnd.close()
            return

        _LOG.info(f"Now viewing: {map_name}")
        self.load_new_map(cache_dir, textures_dir, new_manifest)

    # -- chunk GPU lifecycle ------------------------------------------------

    def _on_chunk_ready(self, chunk_data):
        vao_list = []
        normal_cache_entry = []
        upload_groups = chunk_data.upload_groups
        if upload_groups is None:
            chunker.prepare_chunk_upload_groups(chunk_data)
            upload_groups = chunk_data.upload_groups or []

        for group in upload_groups:
            active_bytes = (group.smooth_vertex_bytes
                            if self.render_mode_buttons.smooth_shading_enabled
                            else group.flat_vertex_bytes)

            vbo = self.ctx.buffer(active_bytes)
            vao = self.ctx.vertex_array(
                self.program, [(vbo, "3f 2f 3f", "in_position", "in_uv", "in_normal")]
            )
            texture = self.texture_manager.acquire(group.material_name)
            vao_list.append((vao, vbo, group.material_name, texture))
            normal_cache_entry.append((
                group.material_name,
                group.smooth_vertex_bytes,
                group.flat_vertex_bytes,
            ))

        self._chunk_gpu_objects[chunk_data.cell] = vao_list
        self._chunk_normal_cache[chunk_data.cell] = normal_cache_entry

    def _on_chunk_unload(self, cell):
        vao_list = self._chunk_gpu_objects.pop(cell, [])
        for vao, vbo, mat_name, texture in vao_list:
            vao.release()
            vbo.release()
            self.texture_manager.release(mat_name)
        self._chunk_normal_cache.pop(cell, None)

    def _apply_shading_toggle(self) -> None:
        """
        Rewrites the normal columns of every currently-loaded chunk's VBO
        in place to match the current smooth_shading_enabled state -- no
        chunk reload or new GPU objects needed. Both normal variants were
        precomputed by the streaming worker, so this is an instant in-place
        update. Chunks that stream in after this point pick up the new
        state automatically via _on_chunk_ready's active_normals selection.
        """
        smooth = self.render_mode_buttons.smooth_shading_enabled
        for cell, vao_list in self._chunk_gpu_objects.items():
            cache_entries = self._chunk_normal_cache.get(cell)
            if not cache_entries or len(cache_entries) != len(vao_list):
                continue
            for (vao, vbo, mat_name, texture), (cached_mat, smooth_bytes, flat_bytes) in zip(
                    vao_list, cache_entries):
                vbo.write(smooth_bytes if smooth else flat_bytes)

    def _buttons_locked_for_loading(self) -> bool:
        """True while map loading should disable the right-side button block."""
        if not self._has_map_loaded:
            return True
        if not self._initial_chunks_loaded:
            return True
        return self.controls_overlay.is_active and not self.controls_overlay.is_manual_mode

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
    RIGHT_COLUMN_GAP = 14  # vertical gap between each of the 4 blocks (brightness, render distance, global light, buttons)
    RIGHT_COLUMN_BUTTON_GROUP_GAP = 30  # extra gap between View dist and Mesh/Texture/Shade group

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

    def _initial_chunk_load_is_ready(self, stats: dict) -> bool:
        loaded = max(0, int(stats.get("loaded", 0)))
        total_available = max(1, int(stats.get("total_available", 1)))
        max_loaded = max(1, int(getattr(self.world.config, "max_loaded_chunks", self._INITIAL_LOAD_MIN_CHUNKS)))
        needed = min(self._INITIAL_LOAD_MIN_CHUNKS, total_available, max_loaded)
        return loaded >= needed

    def _initial_chunk_load_progress(self, stats: dict) -> float:
        loaded = max(0, int(stats.get("loaded", 0)))
        ready = max(0, int(stats.get("ready", 0)))
        total_available = max(1, int(stats.get("total_available", 1)))
        max_loaded = max(1, int(getattr(self.world.config, "max_loaded_chunks", self._INITIAL_LOAD_MIN_CHUNKS)))
        wanted = max(1, int(stats.get("wanted", self._INITIAL_LOAD_MIN_CHUNKS)))
        needed = min(self._INITIAL_LOAD_MIN_CHUNKS, total_available, max_loaded, wanted)
        return max(0.0, min(1.0, float(loaded + ready) / float(needed)))

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
        if window_size == self._layout_cache_size:
            return self._layout_cache_result

        w, h = window_size

        # label reserve: matches StepperControl.render's own
        # label_size=1.5 text height + 8px gap, computed here once so
        # this stays correct if that label styling ever changes (rather
        # than a second hard-coded guess at the same number).
        from gui import bitmap_font
        label_reserve = bitmap_font.text_height_px(1.5) + 8

        button_block_height = RenderModeButtons.total_stack_height()
        content_right_inset = self.RIGHT_COLUMN_PANEL_RIGHT_MARGIN + self.RIGHT_COLUMN_PANEL_SIDE_PAD
        content_bottom_inset = self.RIGHT_COLUMN_PANEL_BOTTOM_MARGIN + self.RIGHT_COLUMN_PANEL_BOTTOM_PAD

        # Build the stack from the BOTTOM up: button block's bottom sits
        # RIGHT_COLUMN_BOTTOM_MARGIN above the window's bottom edge.
        buttons_bottom_y = h - content_bottom_inset
        buttons_top_y = buttons_bottom_y - button_block_height

        render_distance_bottom_y = buttons_top_y - self.RIGHT_COLUMN_BUTTON_GROUP_GAP
        render_distance_anchor_y = render_distance_bottom_y - self.render_distance_stepper.total_height()

        ambient_bottom_y = render_distance_anchor_y - label_reserve - self.RIGHT_COLUMN_GAP
        ambient_anchor_y = ambient_bottom_y - self.ambient_stepper.total_height()

        brightness_bottom_y = ambient_anchor_y - label_reserve - self.RIGHT_COLUMN_GAP
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
        label_height = bitmap_font.text_height_px(self.RIGHT_COLUMN_PANEL_LABEL_SIZE)
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
        label_tops = [
            brightness_anchor_y - label_height - self.RIGHT_COLUMN_PANEL_LABEL_GAP,
            ambient_anchor_y - label_height - self.RIGHT_COLUMN_PANEL_LABEL_GAP,
            render_distance_anchor_y - label_height - self.RIGHT_COLUMN_PANEL_LABEL_GAP,
        ]

        button_x0, _button_y0, button_x1, _button_y1 = self.render_mode_buttons._button_rect_px(
            0, window_size, buttons_top_y, column["content_right_inset"]
        )
        _last_x0, _last_y0, _last_x1, button_bottom_y = self.render_mode_buttons._button_rect_px(
            5, window_size, buttons_top_y, column["content_right_inset"]
        )

        x0 = min(min(stepper_lefts), button_x0) - self.RIGHT_COLUMN_PANEL_SIDE_PAD
        x1 = w - self.RIGHT_COLUMN_PANEL_RIGHT_MARGIN
        y0 = min(label_tops) - self.RIGHT_COLUMN_PANEL_TOP_PAD
        y1 = h - self.RIGHT_COLUMN_PANEL_BOTTOM_MARGIN
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

    def on_render(self, current_time: float, frame_time: float):
        if self._closing_requested:
            return

        # Keep render-mode button effects synced to loading state even
        # on frames that early-return before normal HUD interaction.
        self._sync_render_mode_loading_policy()

        if self._startup_focus_enabled:
            self._request_startup_focus_once()

        # Backends can miss iconify callbacks on Dock minimize; poll a
        # few common window flags each frame as a safety net.
        runtime_iconified = self._query_runtime_iconified_state()
        self._set_background_pause(runtime_iconified, "runtime window state")

        if self._is_iconified:
            # Keep minimize mode cheap: no streaming updates/uploads while
            # iconified, and gently throttle callback spin to keep CPU low.
            time.sleep(0.12)
            return

        if not self._has_map_loaded:
            # First frame with no map loaded yet: just clear to a neutral
            # background and let this frame actually reach the screen
            # before doing anything else -- _handle_continuous_input,
            # world.update, the camera, etc all assume a loaded map and
            # would crash if touched here. The actual import is kicked
            # off AFTER this first real frame has rendered (see the
            # _pending_import_started check below), specifically so the
            # window is confirmed visibly open first, rather than risking
            # the blocking import starting before anything has actually
            # been drawn to the screen even once.
            self.ctx.clear(0.02, 0.02, 0.03)
            if self._pending_import_started:
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

        dt = max(frame_time, 1e-4)
        self._handle_continuous_input(dt)

        # Apply the render-distance control's current value before the
        # streaming world recalculates this frame -- a click on +/- takes
        # effect immediately rather than waiting for the camera to move
        # (see the matching check in StreamingWorld.update(), which
        # detects a changed load_radius_cells on its own, not just a
        # moved camera -- this assignment is what actually gives it a
        # changed value to detect).
        if self.world.config.load_radius_cells != self.render_distance_stepper.value:
            self.world.config.load_radius_cells = self.render_distance_stepper.value

        t0 = time.perf_counter()
        self.world.update(self.camera.position.astype(np.float32))
        self.world.drain_ready_chunks(
            self._on_chunk_ready, self._on_chunk_unload,
            max_per_frame=self._upload_chunks_per_frame,
            time_budget_ms=self._upload_time_budget_ms,
        )
        streaming_ms = (time.perf_counter() - t0) * 1000.0
        stats = self.world.stats()
        if not self._initial_chunks_loaded and self._initial_chunk_load_is_ready(stats):
            self._initial_chunks_loaded = True

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
                title="Preparing Map", note="",
            )
            return

        if self._chunk_prep_complete_until is not None and now < self._chunk_prep_complete_until:
            _map_name = os.path.basename(self.manifest.get("source_obj", "map"))
            self.import_progress_panel.render(
                self.wnd.size, _map_name, "opening cave", 1.0,
                title="Preparing Map", note="",
            )
            return

        self._chunk_prep_complete_until = None

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

        # GPU timer query wraps both draw passes so the elapsed value
        # reflects actual GPU execution time -- distinct from the CPU
        # wall-clock mesh_draw_ms below, which includes Python loop
        # overhead and driver submission but not necessarily the GPU's
        # own fill/shading cost. Reading .elapsed after the with-block
        # stalls until the GPU result is ready; this is intentional for
        # diagnostic accuracy and adds negligible overhead on modern
        # drivers where the query resolves within the same frame.
        # Frustum-cull loaded chunks against the current view before drawing.
        # Build _visible_cells once so both solid and wireframe passes share
        # the same culled set without repeating the test.
        _vp_planes = self._frustum_planes(view, proj)
        _visible_cells = []
        for cell, vao_list in self._chunk_gpu_objects.items():
            aabb = self._chunk_aabbs.get(cell)
            if aabb is None or self._aabb_inside_frustum(_vp_planes, aabb[0], aabb[1]):
                _visible_cells.append((cell, vao_list))
        _chunks_drawn = len(_visible_cells)

        # u_texture always refers to sampler unit 0 -- set it once before
        # the loop rather than redundantly on every single draw call.
        self.program["u_texture"].value = 0
        with self.ctx.query(time=True) as _gpu_q:
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

        self._last_gpu_draw_ms = _gpu_q.elapsed / 1_000_000
        mesh_draw_ms = (time.perf_counter() - t0) * 1000.0

        # Overlay HUD elements draw last, on top of the 3D scene, each with
        # their own depth-disabled 2D pass.
        t0 = time.perf_counter()

        # Whole right-side column -- brightness, global light, render
        # distance, then the Mesh/Texture/Help/Color/Open buttons -- is
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

        self.minimap.render(self.wnd.size, self.camera.position, self.camera.forward())

        self.render_mode_buttons.render(self.wnd.size, buttons_top_y,
                          help_active=self.controls_overlay.is_manual_mode,
                          color_active=self.color_picker.is_active,
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
            _LOG.warning(f"FRAME SPIKE: {total_ms:.1f}ms (avg {rolling_avg:.1f}ms) | "
                         f"streaming={streaming_ms:.1f}ms mesh_draw={mesh_draw_ms:.1f}ms "
                         f"gpu_draw={self._last_gpu_draw_ms:.1f}ms "
                         f"overlay={overlay_ms:.1f}ms | drawn={_chunks_drawn}/{len(self._chunk_gpu_objects)} "
                         f"loaded={stats['loaded']} pending={stats['pending']}")

        self._frame_active_time_s += (total_ms / 1000.0)
        self._frame_count += 1
        now = time.time()
        if now - self._last_fps_print > 2.0:
            wall_interval_s = max(now - self._last_fps_print, 1e-6)
            active_interval_s = max(self._frame_active_time_s, 1e-6)
            rendered_fps = self._frame_count / active_interval_s
            wall_fps = self._frame_count / wall_interval_s
            stats = self.world.stats()
            _LOG.info(f"rendered_fps={rendered_fps:.1f} wall_fps={wall_fps:.1f} "
                      f"frame_cost={rolling_avg:.1f}ms "
                      f"| chunks loaded={stats['loaded']} "
                      f"pending={stats['pending']} drawn={_chunks_drawn}/{len(self._chunk_gpu_objects)} "
                      f"| speed={self.camera.move_speed:.1f}m/s "
                      f"| gpu_draw={self._last_gpu_draw_ms:.1f}ms")
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

        # Raw backend modifiers are the most reliable source on macOS.
        if sys.platform == "darwin" and self._raw_command_modifier_down():
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
        if sys.platform == "darwin":
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

    def _bookmark_save_modifier_is_down(self, modifiers: KeyModifiers) -> bool:
        """Check if the platform-specific bookmark save modifier is down."""
        save_modifier = self._platform_adapter.bookmark_save_modifier()
        if save_modifier == "command":
            return self._command_is_down(modifiers)
        elif save_modifier == "control":
            return self._control_is_down(modifiers)
        return False

    def _load_bookmarks(self) -> None:
        self._bookmarks = {}
        if not self._bookmarks_path:
            return
        if not os.path.exists(self._bookmarks_path):
            return
        try:
            with open(self._bookmarks_path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            slots = raw.get("slots", {}) if isinstance(raw, dict) else {}
            for slot_str, payload in slots.items():
                slot = int(slot_str)
                if slot < 1 or slot > 9:
                    continue
                if not isinstance(payload, dict):
                    continue
                pos = payload.get("position")
                yaw = payload.get("yaw")
                pitch = payload.get("pitch")
                if isinstance(pos, list) and len(pos) == 3 and yaw is not None and pitch is not None:
                    self._bookmarks[slot] = {
                        "position": [float(pos[0]), float(pos[1]), float(pos[2])],
                        "yaw": float(yaw),
                        "pitch": float(pitch),
                    }
        except Exception as e:
            _LOG.warning(f"Failed to load bookmarks: {e}")

    def _save_bookmarks(self) -> None:
        if not self._bookmarks_path:
            return
        try:
            payload = {
                "version": 1,
                "slots": {str(slot): data for slot, data in sorted(self._bookmarks.items())},
            }
            with open(self._bookmarks_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2)
        except Exception as e:
            _LOG.warning(f"Failed to save bookmarks: {e}")

    def _save_bookmark_slot(self, slot: int) -> None:
        if not self._has_map_loaded:
            return
        self._bookmarks[slot] = {
            "position": [
                float(self.camera.position[0]),
                float(self.camera.position[1]),
                float(self.camera.position[2]),
            ],
            "yaw": float(self.camera.yaw),
            "pitch": float(self.camera.pitch),
        }
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
        self.camera.yaw = float(data["yaw"])
        pitch = float(data["pitch"])
        pitch_limit = getattr(self.camera, "_pitch_limit", None)
        if pitch_limit is not None:
            pitch = max(-float(pitch_limit), min(float(pitch_limit), pitch))
        self.camera.pitch = pitch
        self.camera.roll = 0.0  # Reset roll when loading a bookmark

        _LOG.info(f"Recalled camera bookmark {slot}.")
        return True

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

        if save_modifier_down or (sys.platform == "darwin" and shift_down):
            self._save_bookmark_slot(slot)
            return True

        self._recall_bookmark_slot(slot)
        return True

    def _option_look_active(self) -> bool:
        if sys.platform != "darwin":
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
            self.camera.move(forward_amt, right_amt, up_amt, dt, speed_mult)

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
        keys = self.wnd.keys
        if action == keys.ACTION_PRESS:
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
            if self._handle_reset_view_shortcut(key, modifiers):
                return
            self._keys_down.add(key)
        elif action == keys.ACTION_RELEASE:
            self._keys_down.discard(key)

    key_event = on_key_event

    def _handle_reset_view_shortcut(self, key, modifiers: KeyModifiers) -> bool:
        """Handle CMD+0 (macOS) or CTRL+0 (Windows/Linux) to reset view."""
        keys = self.wnd.keys
        
        # Check if this is the 0 key
        if not self._is_zero_key(keys, key):
            return False
        
        # Check platform-specific modifier
        if sys.platform == "darwin":
            # macOS: check for Command key via modifiers
            if self._command_is_down(modifiers):
                self.camera.reset_view()
                return True
        else:
            # Windows/Linux: check for Control key via modifiers
            if self._control_is_down(modifiers):
                self.camera.reset_view()
                return True
        
        return False

    def _request_startup_focus_once(self) -> None:
        """Attempt to bring the app window to foreground once after startup."""
        if self._startup_focus_requested:
            return
        self._startup_focus_requested = True

        # macOS window managers can re-place windows when visibility/frontmost
        # is forced too aggressively (appears at top-left, then jumps). Prefer
        # a minimal one-shot activate path and avoid set_visible/AppleScript.
        if sys.platform == "darwin":
            for target in (getattr(self.wnd, "_window", None), self.wnd):
                if target is None:
                    continue
                try:
                    if hasattr(target, "activate"):
                        target.activate()
                        break
                except Exception:
                    pass
                try:
                    if hasattr(target, "switch_to"):
                        target.switch_to()
                        break
                except Exception:
                    pass
            return

        # Non-macOS: keep broader compatibility with backend APIs.
        for target in (self.wnd, getattr(self.wnd, "_window", None)):
            if target is None:
                continue
            try:
                if hasattr(target, "switch_to"):
                    target.switch_to()
            except Exception:
                pass
            try:
                if hasattr(target, "activate"):
                    target.activate()
            except Exception:
                pass

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
        # Color picker's RGB sliders still use continuous drag (a
        # separate feature from the brightness/render-distance controls
        # below, which were converted to discrete +/- steppers) -- this
        # still needs to take priority over camera look while one of its
        # sliders is being dragged, same reasoning as before.
        if self.color_picker.is_dragging:
            self.color_picker.on_mouse_drag(x, y, self.wnd.size)
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

        look_button_name = self._platform_adapter.mouse_look_button_name()
        look_button = self.wnd.mouse.left if look_button_name == "left" else self.wnd.mouse.right

        if button == self.wnd.mouse.left:
            # macOS-friendly mouse-look: Option + left-drag avoids relying
            # on right-click behavior (which can vary across trackpads/mice).
            if sys.platform == "darwin":
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
                # find_landing_position in core/chunker.py). This is what
                # prevents landing above or below the actual passage.
                landing_x, landing_y, landing_z = chunker.find_landing_position(
                    self.manifest, target_x, target_z,
                    preferred_y=float(self.camera.position[1]),
                )
                self.camera.position[0] = landing_x
                self.camera.position[1] = landing_y
                self.camera.position[2] = landing_z

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
        look_button_name = self._platform_adapter.mouse_look_button_name()
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

    def on_close(self):
        if self._closing_requested:
            return
        self._closing_requested = True

        if hasattr(self, "wnd"):
            try:
                self.wnd.mouse_exclusivity = False
            except Exception:
                pass

        if self._has_map_loaded:
            self._teardown_current_map()
        self._release_window_resources()

        # Ensure the backend window loop receives an explicit close request.
        if hasattr(self, "wnd") and hasattr(self.wnd, "close"):
            try:
                self.wnd.close()
            except Exception:
                pass

    close = on_close



def run_viewer(cache_dir: str, textures_dir: str):
    manifest = chunker.load_manifest(cache_dir)

    # Set as class attributes rather than passing through run_window_config's
    # kwargs -- see the comment on CaveViewerWindow's class attributes above
    # for why. This sidesteps moderngl-window version differences in how
    # (or whether) run_window_config forwards extra keyword arguments.
    CaveViewerWindow.cave_cache_dir = cache_dir
    CaveViewerWindow.cave_textures_dir = textures_dir
    CaveViewerWindow.cave_manifest = manifest

    mglw.run_window_config(CaveViewerWindow, args=[])


def run_viewer_with_pending_import(model_descriptor: dict, textures_dir: str):
    """
    Launches the viewer window for a map that needs FIRST-TIME import
    (no _cache yet) -- used by caveviewer.py's main() instead
    of run_viewer() specifically so the import can run AFTER the window
    is open, showing real progress in the same in-window panel the OPEN
    button already uses, rather than the old behavior of running the
    import entirely before any window existed (which could only show a
    plain console progress bar, with nowhere graphical to draw into yet).

    model_descriptor is whatever caveviewer.py's find_model_file()
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
        mglw.run_window_config(CaveViewerWindow, args=[])
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
