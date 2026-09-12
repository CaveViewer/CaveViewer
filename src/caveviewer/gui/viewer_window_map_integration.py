"""Render-thread map, import, and streaming integration for the viewer window."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import math
import os
import time

import numpy as np

from caveviewer.core.chunking import builder as chunker
from caveviewer.core.map import slicing as map_slicing
from caveviewer.core.hardware import gpu_memory, memory_targets, system_memory
from caveviewer.core.diagnostics.logging import get_logger
from caveviewer.core.streaming.world import StreamingConfig, StreamingWorld
from caveviewer.gui import recorded_dive, render_upload, view_culling, viewer_streaming_runtime
from caveviewer.gui.camera import FlyCamera
from caveviewer.gui.chunk_upload import ChunkUploadManager
from caveviewer.gui.map_opening import pick_folder_dialog, resolve_selected_map_folder
from caveviewer.gui.map_opening_progress import MapOpeningProgressFrame, MapOpeningProgressSession
from caveviewer.gui.minimap import Minimap
from caveviewer.gui.platform import DesktopServiceError
from caveviewer.gui.texture_manager import TextureManager
from caveviewer.gui.viewer_map_runtime import ViewerMapRuntime
from caveviewer.gui.viewer_streaming_runtime import ViewerStreamingRuntime
from caveviewer.gui.viewer_window_sizing import (
    DEFAULT_WINDOW_SIZE as _DEFAULT_WINDOW_SIZE,
    viewer_ui_surface_size as _viewer_ui_surface_size,
)

_LOG = get_logger("CaveViewer")
_TEXTURE_RESIDENT_CACHE_MB_ENV = "CAVEVIEWER_TEXTURE_RESIDENT_CACHE_MB"
_GPU_RESIDENCY_SAFETY_SHARE = 0.05
_RENDER_UPLOAD_INITIAL_SLICE_BYTES = render_upload.RENDER_UPLOAD_INITIAL_SLICE_BYTES
_CATCHUP_UPLOAD_CHUNKS_PER_FRAME = 2
_CATCHUP_UPLOAD_OPERATIONS_PER_CHUNK = 8
_CATCHUP_UPLOAD_TIME_BUDGET_MS = 8.0
_STARTUP_UPLOAD_CHUNKS_PER_FRAME = 4
_STARTUP_UPLOAD_OPERATIONS_PER_CHUNK = 8
_STARTUP_UPLOAD_TIME_BUDGET_MS = 12.0
_VIEWER_STREAMING_SHUTDOWN_TIMEOUT_SECONDS = 2.0
_MAIN_THREAD_STALL_LOG_THRESHOLD_S = 0.5
_MAIN_THREAD_STALL_LOG_MIN_INTERVAL_S = 2.0


@dataclass(frozen=True)
class _MapResourceBudget:
    streaming_settings: object | None
    gpu_vendor: str
    gpu_memory_bytes: int | None
    gpu_target_fraction: float
    max_texture_dimension: int
    max_decoded_cache_bytes: int
    max_resident_texture_bytes: int
    gpu_geometry_budget_bytes: int | None

def _env_optional_mebibytes(name: str) -> int | None:
    raw = str(os.environ.get(name, "")).strip()
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError:
        return None
    return max(1, value)

def _map_initial_camera_position(manifest: Mapping[str, object]) -> np.ndarray:
    """Return a slice entry point or the ordinary render-cache start."""
    slice_metadata = manifest.get(map_slicing.SLICE_MANIFEST_KEY)
    if isinstance(slice_metadata, Mapping):
        try:
            entry_position = np.asarray(
                tuple(float(value) for value in slice_metadata["entry_position"]),
                dtype=np.float64,
            )
        except (KeyError, TypeError, ValueError):
            entry_position = np.empty(0, dtype=np.float64)
        if entry_position.shape == (3,) and np.isfinite(entry_position).all():
            return entry_position
    position = chunker.first_manifest_chunk_center(manifest.get("chunks"))
    if position is None:
        raise ValueError("map manifest does not contain a valid starting chunk")
    return np.asarray(position, dtype=np.float64)

def _normalize_map_root(map_root: str | os.PathLike[str] | None) -> str | None:
    if map_root is None:
        return None
    raw_map_root = os.fspath(map_root).strip()
    if not raw_map_root:
        return None
    return os.path.abspath(os.path.expanduser(raw_map_root))

def _import_controller_property(attribute_name: str):
    def getter(self):
        return getattr(self._ensure_import_controller(), attribute_name)

    def setter(self, value) -> None:
        setattr(self._ensure_import_controller(), attribute_name, value)

    return property(getter, setter)

def _map_runtime_property(attribute_name: str):
    """Bridge transitional window attributes to the authoritative map owner."""

    def runtime(window) -> ViewerMapRuntime:
        value = getattr(window, "_map_runtime", None)
        if value is None:
            value = ViewerMapRuntime()
            window._map_runtime = value
        return value

    def getter(window):
        return getattr(runtime(window), attribute_name)

    def setter(window, value):
        setattr(runtime(window), attribute_name, value)

    return property(getter, setter)

def _streaming_runtime_property(attribute_name: str):
    """Bridge transitional readiness attributes to the streaming owner."""

    def runtime(window) -> ViewerStreamingRuntime:
        value = getattr(window, "_streaming_runtime", None)
        if value is None:
            value = ViewerStreamingRuntime()
            window._streaming_runtime = value
        return value

    def getter(window):
        return getattr(runtime(window), attribute_name)

    def setter(window, value) -> None:
        setattr(runtime(window), attribute_name, value)

    return property(getter, setter)


class ViewerWindowMapIntegration:
    """Methods executed by ``CaveViewerWindow`` on its owning callback thread."""

    _import_active = _import_controller_property("active")
    _import_is_startup = _import_controller_property("is_startup")
    _import_thread = _import_controller_property("thread")
    _import_process = _import_controller_property("process")
    _import_command_queue = _import_controller_property("command_queue")
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
    cache_dir = _map_runtime_property("cache_dir")
    textures_dir = _map_runtime_property("textures_dir")
    map_root = _map_runtime_property("map_root")
    manifest = _map_runtime_property("manifest")
    world = _map_runtime_property("world")
    camera = _map_runtime_property("camera")
    minimap = _map_runtime_property("minimap")
    texture_manager = _map_runtime_property("texture_manager")
    _chunk_upload_manager = _map_runtime_property("chunk_upload_manager")
    _chunk_gpu_objects = _map_runtime_property("chunk_gpu_objects")
    _chunk_upload_states = _map_runtime_property("chunk_upload_states")
    _chunk_normal_cache = _map_runtime_property("chunk_normal_cache")
    _chunk_aabbs = _map_runtime_property("chunk_aabbs")
    _view_culling_cache = _map_runtime_property("view_culling_cache")
    _chunk_visibility_generation = _map_runtime_property(
        "chunk_visibility_generation"
    )
    _has_map_loaded = _map_runtime_property("loaded")
    _initial_chunks_loaded = _streaming_runtime_property("initial_chunks_loaded")
    _initial_visual_ready_logged = _streaming_runtime_property(
        "initial_visual_ready_logged"
    )
    _import_cache_dir = _import_controller_property("cache_dir")
    _texture_validation_executor = _map_runtime_property(
        "texture_validation_executor"
    )
    _texture_validation_future = _map_runtime_property("texture_validation_future")
    _texture_validation_manager = _map_runtime_property(
        "texture_validation_manager"
    )
    _texture_validation_cache_dir = _map_runtime_property(
        "texture_validation_cache_dir"
    )
    _texture_validation_started_at = _map_runtime_property(
        "texture_validation_started_at"
    )
    _initial_visual_ready = _streaming_runtime_property("initial_visual_ready")
    _initial_visual_ready_frames = _streaming_runtime_property(
        "initial_visual_ready_frames"
    )
    _initial_visual_ready_visible_chunks = _streaming_runtime_property(
        "initial_visual_ready_visible_chunks"
    )
    _initial_visual_ready_required_textures = _streaming_runtime_property(
        "initial_visual_ready_required_textures"
    )
    _initial_visual_ready_resident_textures = _streaming_runtime_property(
        "initial_visual_ready_resident_textures"
    )
    _initial_visual_ready_visible_textures = _streaming_runtime_property(
        "initial_visual_ready_visible_textures"
    )
    _initial_visual_ready_missing_textures = _streaming_runtime_property(
        "initial_visual_ready_missing_textures"
    )
    _initial_visual_ready_expected_chunks = _streaming_runtime_property(
        "initial_visual_ready_expected_chunks"
    )
    _initial_visual_ready_covered_chunks = _streaming_runtime_property(
        "initial_visual_ready_covered_chunks"
    )
    _initial_visual_ready_missing_chunks = _streaming_runtime_property(
        "initial_visual_ready_missing_chunks"
    )
    _initial_visual_ready_coverage_pct = _streaming_runtime_property(
        "initial_visual_ready_coverage_pct"
    )
    _initial_route_prefetch_expected_cells = _streaming_runtime_property(
        "initial_route_prefetch_expected_cells"
    )
    _initial_route_prefetch_loaded_cells = _streaming_runtime_property(
        "initial_route_prefetch_loaded_cells"
    )
    _initial_route_prefetch_pending_cells = _streaming_runtime_property(
        "initial_route_prefetch_pending_cells"
    )
    _initial_route_prefetch_failed_cells = _streaming_runtime_property(
        "initial_route_prefetch_failed_cells"
    )
    _initial_route_prefetch_missing_cells = _streaming_runtime_property(
        "initial_route_prefetch_missing_cells"
    )
    _initial_route_prefetch_coverage_pct = _streaming_runtime_property(
        "initial_route_prefetch_coverage_pct"
    )

    def _load_map(
        self,
        cache_dir: str,
        textures_dir: str,
        manifest: dict,
        *,
        map_root: str | os.PathLike[str] | None = None,
    ) -> None:
        """
        Sets up everything specific to ONE map: the texture manager, the
        streaming world, the starting camera position, and the minimap.
        Called once from __init__ for the map the program launched with,
        and called again from load_new_map() when switching to a
        different map via the OPEN button -- _teardown_current_map() must
        be called first in that second case, to cleanly release the
        previous map's GPU/thread resources before this builds new ones.
        """
        runtime = ViewerMapRuntime.for_map(
            cache_dir=cache_dir,
            textures_dir=textures_dir,
            map_root=_normalize_map_root(map_root),
            manifest=manifest,
        )
        self._map_runtime = runtime
        pending_recorded_dive = getattr(
            self, "_pending_recorded_dive_trace", None
        )
        try:
            self._initialize_map_runtime()
        except BaseException:
            try:
                runtime.release(
                    streaming_shutdown_timeout=(
                        _VIEWER_STREAMING_SHUTDOWN_TIMEOUT_SECONDS
                    ),
                    logger=_LOG,
                )
            except Exception:
                _LOG.exception("Error while cleaning up a failed map load.")
            self._recorded_dive_trace = None
            self._recorded_dive_controller = None
            self._pending_recorded_dive_trace = pending_recorded_dive
            if self._map_runtime is runtime:
                self._map_runtime = ViewerMapRuntime()
            raise
        runtime.loaded = True

    def _initialize_map_runtime(self) -> None:
        load_started_at = time.perf_counter()
        pending_recorded_dive = getattr(self, "_pending_recorded_dive_trace", None)
        if pending_recorded_dive is not None:
            recorded_dive.validate_recorded_dive_manifest(
                pending_recorded_dive,
                self.manifest,
            )
        self._initial_compilation_started_at = time.perf_counter()
        self._initial_compilation_logged = False

        budget = self._map_resource_budget()
        predecode_textures = self._initialize_map_texture_manager(budget)
        benchmark_controller = self._initialize_map_streaming_world(
            budget,
            predecode_textures=predecode_textures,
        )
        self._initialize_map_camera(
            pending_recorded_dive,
            benchmark_controller=benchmark_controller,
        )
        self._initialize_map_render_resources()
        self.controls_overlay.show_fullscreen()
        self._reset_initial_chunk_loading_state()
        self._record_benchmark_streaming_environment()
        self._log_main_thread_stall(
            "map load",
            time.perf_counter() - load_started_at,
            chunks=len(self.manifest.get("chunks", {})),
        )

    def _map_resource_budget(self) -> _MapResourceBudget:
        viewer_settings = self._viewer_runtime_settings
        streaming_settings = (
            viewer_settings.streaming if viewer_settings is not None else None
        )
        gpu_vendor = str(self.ctx.info.get("GL_VENDOR", ""))
        gpu_memory_bytes = gpu_memory.detect_total_gpu_memory_bytes(
            gpu_vendor,
            logger=_LOG,
            environment=(
                {
                    "CAVEVIEWER_GPU_MEMORY_GB": str(streaming_settings.gpu_memory_gb)
                }
                if streaming_settings is not None
                and streaming_settings.gpu_memory_gb is not None
                else ({} if streaming_settings is not None else None)
            ),
        )
        gpu_target_fraction = (
            memory_targets.parse_gpu_target_fraction(
                os.environ.get("CAVEVIEWER_GPU_MEMORY_UTILIZATION_TARGET")
            )
            if streaming_settings is None
            else max(
                0.01,
                min(
                    0.80,
                    float(streaming_settings.gpu_memory_target_percent) / 100.0,
                ),
            )
        )
        max_texture_dimension = TextureManager.recommend_max_texture_dimension(
            self.manifest["mtl_materials"],
            gpu_memory_bytes,
            gpu_target_fraction,
            configured_limit=(
                viewer_settings.max_texture_dimension
                if viewer_settings is not None
                else None
            ),
            use_environment_override=viewer_settings is None,
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
        resident_cap = (
            _env_optional_mebibytes(_TEXTURE_RESIDENT_CACHE_MB_ENV)
            if streaming_settings is None
            else (
                max(1, int(streaming_settings.texture_resident_cache_mb * 1024**2))
                if streaming_settings.texture_resident_cache_mb is not None
                else None
            )
        )
        if resident_cap is not None:
            max_resident_texture_bytes = min(max_resident_texture_bytes, resident_cap)
            cap_source = (
                f"{_TEXTURE_RESIDENT_CACHE_MB_ENV}="
                f"{os.environ.get(_TEXTURE_RESIDENT_CACHE_MB_ENV, '').strip()} MB"
                if streaming_settings is None
                else "composed runtime settings"
            )
            _LOG.info(
                "Texture resident GPU LRU cache capped by %s: %.1f MB.",
                cap_source,
                max_resident_texture_bytes / (1024**2),
            )

        gpu_geometry_budget_bytes = None
        if gpu_memory_bytes is not None and gpu_memory_bytes > 0:
            total_budget = int(gpu_memory_bytes * gpu_target_fraction)
            max_resident_texture_bytes = min(max_resident_texture_bytes, total_budget)
            safety_bytes = min(
                max(0, total_budget - max_resident_texture_bytes),
                int(total_budget * _GPU_RESIDENCY_SAFETY_SHARE),
            )
            gpu_geometry_budget_bytes = max(
                0,
                total_budget - max_resident_texture_bytes - safety_bytes,
            )
            _LOG.info(
                "GPU residency budget split: target %.1f MB (%.0f%% of %.1f GB); "
                "textures %.1f MB, geometry %.1f MB, safety %.1f MB.",
                total_budget / (1024**2),
                gpu_target_fraction * 100.0,
                gpu_memory_bytes / (1024**3),
                max_resident_texture_bytes / (1024**2),
                gpu_geometry_budget_bytes / (1024**2),
                safety_bytes / (1024**2),
            )
        return _MapResourceBudget(
            streaming_settings=streaming_settings,
            gpu_vendor=gpu_vendor,
            gpu_memory_bytes=gpu_memory_bytes,
            gpu_target_fraction=gpu_target_fraction,
            max_texture_dimension=max_texture_dimension,
            max_decoded_cache_bytes=max_decoded_cache_bytes,
            max_resident_texture_bytes=max_resident_texture_bytes,
            gpu_geometry_budget_bytes=gpu_geometry_budget_bytes,
        )

    def _initialize_map_texture_manager(
        self,
        budget: _MapResourceBudget,
    ) -> Callable[[object], None]:
        started_at = time.perf_counter()
        self.texture_manager = TextureManager(
            self.ctx,
            self.textures_dir,
            self.manifest["mtl_materials"],
            max_texture_dimension=budget.max_texture_dimension,
            max_decoded_cache_bytes=budget.max_decoded_cache_bytes,
            max_resident_texture_bytes=budget.max_resident_texture_bytes,
        )
        self._log_main_thread_stall(
            "texture manager setup",
            time.perf_counter() - started_at,
            materials=len(self.manifest.get("mtl_materials", {})),
        )
        self._start_texture_validation_async()

        def predecode_textures_for_chunk(chunk_data) -> None:
            for group in chunk_data.groups.values():
                self.texture_manager.decode_for_material(group.material_name)

        return predecode_textures_for_chunk

    def _initialize_map_streaming_world(
        self,
        budget: _MapResourceBudget,
        *,
        predecode_textures: Callable[[object], None],
    ):
        chunk_size = chunker.manifest_chunk_size(self.manifest)
        if chunk_size is None:
            raise ValueError(
                "Map cache manifest is missing a valid chunk_size. "
                "Rebuild this map's reported cache directory with this version "
                "of CaveViewer."
            )
        configured_chunk_size = (
            chunker.configured_chunk_size()
            if self._runtime_settings is None
            else float(self._runtime_settings["chunk_size_meters"])
        )
        _LOG.info("Opening map cache with manifest chunk size: %g.", chunk_size)
        if abs(chunk_size - configured_chunk_size) > 1e-6:
            _LOG.info(
                "Current %s setting is %g, but existing/prebuilt caches stream "
                "using the chunk size recorded in manifest.json.",
                chunker.CHUNK_SIZE_ENV_VAR,
                configured_chunk_size,
            )
        benchmark_controller = self._active_benchmark_controller()
        if benchmark_controller is not None:
            benchmark_radius = int(benchmark_controller.scenario.render_distance)
            clamped_radius = max(
                self.render_distance_stepper.min_value,
                min(self.render_distance_stepper.max_value, benchmark_radius),
            )
            if clamped_radius != benchmark_radius:
                _LOG.warning(
                    "Benchmark render_distance=%d exceeds viewer control range; using %d.",
                    benchmark_radius,
                    clamped_radius,
                )
            self.render_distance_stepper.value = clamped_radius
        config = StreamingConfig(
            chunk_size=chunk_size,
            load_radius_cells=self.render_distance_stepper.value,
            unload_radius_margin=1,
        )
        started_at = time.perf_counter()
        self.world = StreamingWorld(
            self.cache_dir,
            config,
            on_decode_textures=predecode_textures,
            prepack_smooth_shading=bool(
                self.render_mode_buttons.smooth_shading_enabled
            ),
            gpu_vendor=budget.gpu_vendor,
            textures_dir=self.textures_dir,
            total_gpu_memory_bytes=budget.gpu_memory_bytes,
            texture_gpu_budget_bytes=budget.max_resident_texture_bytes,
            gpu_geometry_budget_bytes=budget.gpu_geometry_budget_bytes,
            manifest=self.manifest,
            estimate_texture_gpu_bytes=False,
            runtime_settings=budget.streaming_settings,
        )
        self._log_main_thread_stall(
            "streaming world setup",
            time.perf_counter() - started_at,
            chunks=len(self.manifest.get("chunks", {})),
        )
        return benchmark_controller

    def _initialize_map_camera(
        self,
        pending_recorded_dive,
        *,
        benchmark_controller,
    ) -> None:
        start_pos = (
            np.asarray(pending_recorded_dive.initial_pose.position, dtype=np.float64)
            if pending_recorded_dive is not None
            else _map_initial_camera_position(self.manifest)
        )
        self.camera = FlyCamera(position=tuple(start_pos))
        self._benchmark_route_prefetch_cells = frozenset()
        if benchmark_controller is not None:
            benchmark_controller.set_position_origin(start_pos)
            benchmark_controller.apply_initial_camera(self.camera)
            self._configure_benchmark_route_prefetch(start_pos)
        elif pending_recorded_dive is not None:
            controller = recorded_dive.RecordedDivePlaybackController(
                pending_recorded_dive
            )
            controller.start(self.camera, now=time.perf_counter())
            self._recorded_dive_trace = pending_recorded_dive
            self._recorded_dive_controller = controller
            self._pending_recorded_dive_trace = None
            self._refresh_recorded_dive_prefetch()
        self._bookmarks_path = os.path.join(self.cache_dir, "camera_bookmarks.json")
        self._load_bookmarks()

    def _initialize_map_render_resources(self) -> None:
        minimap_started_at = time.perf_counter()
        self.minimap = Minimap(self.ctx, self.manifest)
        self._log_main_thread_stall(
            "minimap setup",
            time.perf_counter() - minimap_started_at,
            chunks=len(self.manifest.get("chunks", {})),
        )
        self._print_texture_diagnostics(self.manifest, self.textures_dir)
        self._chunk_gpu_objects = {}
        self._chunk_upload_states = {}
        self._chunk_normal_cache = {}
        self._chunk_aabbs = {}
        self._view_culling_cache = view_culling.FrustumCullingCache()
        self._chunk_visibility_generation = 0
        self._chunk_upload_manager = ChunkUploadManager(
            ctx=self.ctx,
            program=self.program,
            texture_manager=self.texture_manager,
            smooth_shading_enabled=lambda: bool(
                self.render_mode_buttons.smooth_shading_enabled
            ),
            gpu_objects=self._chunk_gpu_objects,
            upload_states=self._chunk_upload_states,
            normal_cache=self._chunk_normal_cache,
            aabbs=self._chunk_aabbs,
            upload_operations_per_chunk=self._current_upload_operations_per_chunk,
            upload_time_budget_ms=self._current_upload_time_budget_ms,
            vbo_upload_slice_bytes=self._vbo_upload_slice_bytes,
            texture_upload_slice_bytes=self._texture_upload_slice_bytes,
        )
        if hasattr(self, "render_distance_stepper"):
            self.world.config.load_radius_cells = self.render_distance_stepper.value

    def _start_texture_validation_async(self) -> bool:
        """
        Start CPU/disk-only texture validation off the render thread.

        Texture validation opens texture headers and checks paths for every
        referenced material.  That is useful diagnostics, but doing it inside
        _load_map() can keep the window event loop from responding long enough
        for the desktop shell to report "application not responding."
        """
        return self._map_runtime.start_texture_validation(
            executor_factory=ThreadPoolExecutor,
            perf_counter=time.perf_counter,
            logger=_LOG,
        )

    def _update_texture_validation(self) -> None:
        self._map_runtime.finish_texture_validation(
            perf_counter=time.perf_counter,
            logger=_LOG,
        )

    def _cancel_texture_validation(self) -> bool:
        future = self._map_runtime.texture_validation_future
        if future is None:
            return False
        self._map_runtime.cancel_texture_validation()
        return True

    def _configure_benchmark_route_prefetch(self, origin: np.ndarray) -> None:
        """Ask streaming to keep the benchmark route tube wanted during startup."""
        benchmark_controller = self._active_benchmark_controller()
        world = getattr(self, "world", None)
        if benchmark_controller is None or world is None:
            return

        radius = max(1, int(getattr(world.config, "load_radius_cells", 1)))
        route_cells: set[tuple[int, int, int]] = set()
        route_positions = tuple(
            self._benchmark_route_sample_positions(
                benchmark_controller.scenario,
                origin,
            )
        )
        for position in route_positions:
            route_cell = world.cell_for_position(np.asarray(position, dtype=np.float32))
            route_cells.update(world.available_cells_in_radius(route_cell, radius))

        self._benchmark_route_prefetch_cells = frozenset(route_cells)
        set_prefetch = getattr(world, "set_prefetch_wanted_cells", None)
        if callable(set_prefetch):
            set_prefetch(route_cells)
        _LOG.info(
            "Benchmark route prefetch enabled: %d cells from %d sampled route "
            "position(s), radius=%d.",
            len(route_cells),
            len(route_positions),
            radius,
        )
        benchmark_controller.update_environment(
            {
                "benchmark_route_prefetch_cells": len(route_cells),
                "benchmark_route_prefetch_sample_positions": len(route_positions),
                "benchmark_route_prefetch_radius_chunks": radius,
            }
        )

    def _benchmark_route_sample_positions(
        self,
        scenario,
        origin: np.ndarray,
    ) -> Iterable[np.ndarray]:
        """Yield absolute route positions densely enough to prefetch the route."""
        world = getattr(self, "world", None)
        chunk_size = max(
            1e-6,
            float(getattr(getattr(world, "config", None), "chunk_size", 1.0)),
        )
        origin_array = np.asarray(origin, dtype=np.float64)
        route = tuple(getattr(scenario, "route", ()))
        if not route:
            return

        absolute_positions = [
            self._benchmark_absolute_route_position(scenario, keyframe, origin_array)
            for keyframe in route
        ]
        previous = absolute_positions[0]
        yield previous
        for current in absolute_positions[1:]:
            segment = current - previous
            distance = float(np.linalg.norm(segment))
            steps = max(1, int(math.ceil(distance / chunk_size)))
            for step in range(1, steps + 1):
                t = step / steps
                yield previous + segment * t
            previous = current

    @staticmethod
    def _benchmark_absolute_route_position(
        scenario,
        keyframe,
        origin: np.ndarray,
    ) -> np.ndarray:
        position = np.asarray(keyframe.position, dtype=np.float64)
        if getattr(scenario, "position_mode", "absolute") == "first_chunk_center_offset":
            return origin + position
        return position

    def _record_benchmark_streaming_environment(self) -> None:
        """Persist effective Streaming/texture settings for benchmark artifacts."""
        benchmark_controller = self._active_benchmark_controller()
        world = getattr(self, "world", None)
        if benchmark_controller is None or world is None:
            return

        config = getattr(world, "config", None)
        texture_manager = getattr(self, "texture_manager", None)
        ready_backlog_capacity = getattr(world, "_ready_backlog_capacity", None)
        worker_target = getattr(world, "_worker_pool_size", None)
        active_workers = getattr(world, "_workers", ())
        update = {
            "effective_render_distance_chunks": int(
                getattr(config, "load_radius_cells", 0) or 0
            ),
            "streaming_chunk_size_m": float(
                getattr(config, "chunk_size", 0.0) or 0.0
            ),
            "streaming_unload_radius_margin": int(
                getattr(config, "unload_radius_margin", 0) or 0
            ),
            "streaming_max_loaded_chunks": int(
                getattr(config, "max_loaded_chunks", 0) or 0
            ),
            "streaming_ready_backlog_capacity": (
                None
                if ready_backlog_capacity is None
                else int(ready_backlog_capacity)
            ),
            "streaming_worker_target": (
                None if worker_target is None else int(worker_target)
            ),
            "streaming_active_workers_at_load": len(tuple(active_workers or ())),
            "benchmark_route_prefetch_cells": int(
                len(getattr(self, "_benchmark_route_prefetch_cells", ()))
            ),
            "upload_chunks_per_frame_effective": int(self._upload_chunks_per_frame),
            "upload_groups_per_frame_effective": int(self._upload_groups_per_frame),
            "upload_time_budget_ms_effective": float(self._upload_time_budget_ms),
            "startup_upload_chunks_per_frame": max(
                self._upload_chunks_per_frame,
                _STARTUP_UPLOAD_CHUNKS_PER_FRAME,
            ),
            "startup_upload_groups_per_frame": max(
                self._upload_groups_per_frame,
                _STARTUP_UPLOAD_OPERATIONS_PER_CHUNK,
            ),
            "startup_upload_time_budget_ms": max(
                self._upload_time_budget_ms,
                _STARTUP_UPLOAD_TIME_BUDGET_MS,
            ),
            "catchup_upload_chunks_per_frame": max(
                self._upload_chunks_per_frame,
                _CATCHUP_UPLOAD_CHUNKS_PER_FRAME,
            ),
            "catchup_upload_groups_per_frame": max(
                self._upload_groups_per_frame,
                _CATCHUP_UPLOAD_OPERATIONS_PER_CHUNK,
            ),
            "catchup_upload_time_budget_ms": max(
                self._upload_time_budget_ms,
                _CATCHUP_UPLOAD_TIME_BUDGET_MS,
            ),
            "texture_max_dimension": (
                None
                if texture_manager is None
                else texture_manager.max_texture_dimension
            ),
            "texture_resident_budget_bytes": (
                None
                if texture_manager is None
                else texture_manager.max_resident_texture_bytes
            ),
            "texture_decoded_cache_budget_bytes": (
                None
                if texture_manager is None
                else texture_manager.max_decoded_cache_bytes
            ),
        }
        benchmark_controller.update_environment(update)

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
        runtime = getattr(self, "_map_runtime", None)
        if runtime is None:
            return

        if runtime.loaded:
            self._stop_manual_dive_trace(
                reason="viewer_closed" if final_shutdown else "map_changed"
            )
            slice_selection = self._ensure_slice_selection_controller()
            if slice_selection.countdown_active:
                slice_selection.cancel_countdown()
                self._clear_slice_context()
            elif slice_selection.selection_active:
                slice_selection.cancel_selection()
                self._clear_slice_context()
            self._stop_recorded_dive(
                reason="viewer_closed" if final_shutdown else "map_changed"
            )
            self._stop_recording()

        # Keep this callback bounded: on_close() runs inside the window/render
        # event path, and an unbounded join here can leave the viewer visually
        # frozen if a streaming worker is stuck in disk or callback code.
        try:
            runtime.release(
                streaming_shutdown_timeout=(
                    _VIEWER_STREAMING_SHUTDOWN_TIMEOUT_SECONDS
                ),
                logger=_LOG,
            )
        finally:
            if self._map_runtime is runtime:
                self._map_runtime = ViewerMapRuntime()
            self._recorded_dive_trace = None
            self._recorded_dive_controller = None

    def _release_window_resources(self) -> None:
        """Release non-map GPU/UI resources when closing the viewer window."""
        if self._window_resources_released:
            return
        self._window_resources_released = True

        self._cancel_texture_validation()
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
        self._load_map(
            cache_dir,
            textures_dir,
            manifest,
            map_root=source_dir,
        )
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
        slice_selection = self._ensure_slice_selection_controller()
        if (
            slice_selection.countdown_active
            or slice_selection.selection_active
            or self._ensure_slice_export_controller().active
        ):
            self._show_capture_status(
                "Slice in progress",
                "Finish or cancel the slice before opening another map.",
                kind="info",
                duration=3.0,
            )
            return
        try:
            folder = pick_folder_dialog(
                platform_runtime=getattr(self, "_platform_runtime", None)
            )
        except DesktopServiceError as exc:
            _LOG.warning("Map folder selection unavailable: %s", exc)
            return
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
            self._ensure_map_opening_progress_session().begin_cached(
                open_target.map_name,
                new_operation=True,
            )
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
            _viewer_ui_surface_size(self.wnd),
        )

    def _render_pending_import_splash(self) -> None:
        pending = self._viewer_session.config.pending_import
        pending_payload = (
            {
                "model_descriptor": pending.model_descriptor,
                "textures_dir": pending.textures_dir,
            }
            if pending is not None
            else None
        )
        self._ensure_import_controller().render_pending_import_splash(
            pending_payload,
            self.import_progress_panel,
            _viewer_ui_surface_size(self.wnd),
            opening_session=self._ensure_map_opening_progress_session(),
        )

    def _ensure_map_opening_progress_session(self) -> MapOpeningProgressSession:
        """Return the GUI-only presentation state for the active map open."""
        session = getattr(self, "_map_opening_progress_session", None)
        if session is None:
            workflows = self.__dict__.get("_workflow_coordinator")
            if workflows is not None:
                return workflows.map_opening
            session = MapOpeningProgressSession()
            self._map_opening_progress_session = session
        return session

    def _render_map_opening_progress(
        self,
        frame: MapOpeningProgressFrame,
    ) -> None:
        """Render one opening frame without giving lifecycle ownership to the panel."""
        self.import_progress_panel.render(
            _viewer_ui_surface_size(self.wnd),
            frame.map_name,
            frame.stage,
            frame.fraction,
            title=frame.title,
            note=frame.note,
            progress_session_id=frame.session_id,
        )

    def _abandon_map_opening_progress(self) -> None:
        """End the presentation session after cancellation, failure, or pause."""
        self._ensure_map_opening_progress_session().abandon()

    def _render_startup_map_load_splash(self) -> None:
        pending = getattr(self, "_startup_map_load_pending", None)
        manifest = pending[2] if pending is not None else {}
        map_name = os.path.basename(str(manifest.get("source_obj", "map")))
        self.ctx.clear(0.02, 0.02, 0.03)
        frame = self._ensure_map_opening_progress_session().begin_cached(map_name)
        self._render_map_opening_progress(frame)

    def _load_startup_map_after_splash(self) -> None:
        pending = getattr(self, "_startup_map_load_pending", None)
        if pending is None:
            return
        if not getattr(self, "_startup_map_load_splash_rendered", False):
            self._render_startup_map_load_splash()
            self._startup_map_load_splash_rendered = True
            return

        self._startup_map_load_pending = None
        cache_dir, textures_dir, manifest, map_root = pending
        self._load_map(
            cache_dir,
            textures_dir,
            manifest,
            map_root=map_root,
        )
        self._has_map_loaded = True

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
        controller = self._ensure_import_controller()
        if not controller.active:
            self._ensure_map_opening_progress_session().begin_import(
                map_name,
                new_operation=not is_startup,
            )
        controller.start_async(
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
        with, when the viewer session carries a pending import instead
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
        pending = self._viewer_session.config.pending_import
        if pending is None:
            raise RuntimeError("The viewer session has no pending import")
        model_descriptor = dict(pending.model_descriptor)
        textures_dir = pending.textures_dir
        source_path = model_descriptor.get("obj_path") or model_descriptor.get("glb_path")
        map_name = os.path.basename(source_path)
        self._start_import_async(model_descriptor, textures_dir, map_name, is_startup=True)

    @staticmethod
    def _new_streaming_frame_timing() -> dict:
        return render_upload.new_streaming_frame_timing()

    @staticmethod
    def _format_optional_ms(value: float | None) -> str:
        return render_upload.format_optional_ms(value)

    @staticmethod
    def _format_streaming_frame_timing(timing: dict) -> str:
        return render_upload.format_streaming_frame_timing(timing)

    def _ensure_chunk_upload_manager(self) -> ChunkUploadManager:
        """Return the render-thread chunk upload owner for the active window."""
        gpu_objects = getattr(self, "_chunk_gpu_objects", None)
        if gpu_objects is None:
            gpu_objects = {}
            self._chunk_gpu_objects = gpu_objects
        upload_states = getattr(self, "_chunk_upload_states", None)
        if upload_states is None:
            upload_states = {}
            self._chunk_upload_states = upload_states
        normal_cache = getattr(self, "_chunk_normal_cache", None)
        if normal_cache is None:
            normal_cache = {}
            self._chunk_normal_cache = normal_cache
        aabbs = getattr(self, "_chunk_aabbs", None)
        if aabbs is None:
            aabbs = {}
            self._chunk_aabbs = aabbs

        ctx = getattr(self, "ctx", None)
        program = getattr(self, "program", None)
        texture_manager = getattr(self, "texture_manager", None)

        def smooth_shading_enabled() -> bool:
            render_mode_buttons = getattr(self, "render_mode_buttons", None)
            return bool(getattr(render_mode_buttons, "smooth_shading_enabled", False))

        manager = getattr(self, "_chunk_upload_manager", None)
        current_time_budget = getattr(
            self,
            "_current_upload_time_budget_ms",
            getattr(self, "_upload_time_budget_ms", 3.0),
        )
        current_operations = getattr(
            self,
            "_current_upload_operations_per_chunk",
            getattr(self, "_upload_groups_per_frame", 1),
        )
        if (
            manager is None
            or manager.ctx is not ctx
            or manager.program is not program
            or manager.texture_manager is not texture_manager
            or manager.gpu_objects is not gpu_objects
            or manager.upload_states is not upload_states
            or manager.normal_cache is not normal_cache
            or manager.aabbs is not aabbs
        ):
            manager = ChunkUploadManager(
                ctx=ctx,
                program=program,
                texture_manager=texture_manager,
                smooth_shading_enabled=smooth_shading_enabled,
                gpu_objects=gpu_objects,
                upload_states=upload_states,
                normal_cache=normal_cache,
                aabbs=aabbs,
                upload_operations_per_chunk=current_operations,
                upload_time_budget_ms=current_time_budget,
                vbo_upload_slice_bytes=getattr(
                    self,
                    "_vbo_upload_slice_bytes",
                    _RENDER_UPLOAD_INITIAL_SLICE_BYTES,
                ),
                texture_upload_slice_bytes=getattr(
                    self,
                    "_texture_upload_slice_bytes",
                    _RENDER_UPLOAD_INITIAL_SLICE_BYTES,
                ),
            )
            self._chunk_upload_manager = manager
        else:
            manager.upload_operations_per_chunk = max(1, int(current_operations))
            manager.upload_time_budget_ms = max(0.5, float(current_time_budget))
        return manager

    def _sync_chunk_upload_state_from_manager(
        self,
        manager: ChunkUploadManager | None = None,
    ) -> None:
        """Mirror manager-owned upload state for legacy private callers/tests."""
        manager = self._ensure_chunk_upload_manager() if manager is None else manager
        self._vbo_upload_slice_bytes = manager.vbo_upload_slice_bytes
        self._texture_upload_slice_bytes = manager.texture_upload_slice_bytes
        self._chunk_gpu_objects = manager.gpu_objects
        self._chunk_upload_states = manager.upload_states
        self._chunk_normal_cache = manager.normal_cache
        self._chunk_aabbs = manager.aabbs

    def _ensure_view_culling_cache(self) -> view_culling.FrustumCullingCache:
        cache = getattr(self, "_view_culling_cache", None)
        if cache is None:
            cache = view_culling.FrustumCullingCache()
            self._view_culling_cache = cache
        if not hasattr(self, "_chunk_visibility_generation"):
            self._chunk_visibility_generation = 0
        return cache

    def _invalidate_visible_chunk_cache(self) -> None:
        self._chunk_visibility_generation = int(
            getattr(self, "_chunk_visibility_generation", 0)
        ) + 1
        cache = getattr(self, "_view_culling_cache", None)
        if cache is not None:
            cache.invalidate()

    @staticmethod
    def _resident_chunk_signature(
        manager: ChunkUploadManager,
        cell,
    ) -> tuple[int, int, bool]:
        vao_list = manager.gpu_objects.get(cell)
        aabb = manager.aabbs.get(cell)
        return (
            id(vao_list) if vao_list is not None else 0,
            len(vao_list) if vao_list is not None else 0,
            aabb is not None,
        )

    def _visible_chunk_gpu_objects(
        self,
        view: np.ndarray,
        projection: np.ndarray,
    ) -> list[tuple[tuple, list]]:
        cache = self._ensure_view_culling_cache()
        return cache.visible_chunks(
            view=view,
            projection=projection,
            chunk_gpu_objects=self._chunk_gpu_objects,
            chunk_aabbs=self._chunk_aabbs,
            generation=self._chunk_visibility_generation,
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
        manager = self._ensure_chunk_upload_manager()
        manager.record_upload_slice_sizes(timing)
        self._sync_chunk_upload_state_from_manager(manager)

    def _adapt_upload_slice_size(
        self,
        *,
        kind: str,
        elapsed_ms: float,
        byte_count: int,
        timing: dict | None,
    ) -> None:
        """Delegate adaptive upload-slice policy to the chunk upload manager."""
        manager = self._ensure_chunk_upload_manager()
        manager.adapt_upload_slice_size(
            kind=kind,
            elapsed_ms=elapsed_ms,
            byte_count=byte_count,
            timing=timing,
        )
        self._sync_chunk_upload_state_from_manager(manager)

    def _on_chunk_ready(self, chunk_data):
        manager = self._ensure_chunk_upload_manager()
        before_signature = self._resident_chunk_signature(manager, chunk_data.cell)
        manager.set_frame_limits(
            operations_per_chunk=getattr(
                self,
                "_current_upload_operations_per_chunk",
                getattr(self, "_upload_groups_per_frame", 1),
            ),
            time_budget_ms=getattr(
                self,
                "_current_upload_time_budget_ms",
                getattr(self, "_upload_time_budget_ms", 3.0),
            ),
            timing=getattr(self, "_streaming_frame_timing", None),
        )
        try:
            return manager.on_chunk_ready(chunk_data)
        finally:
            after_signature = self._resident_chunk_signature(manager, chunk_data.cell)
            if after_signature != before_signature:
                self._invalidate_visible_chunk_cache()
            self._sync_chunk_upload_state_from_manager(manager)

    def _on_chunk_unload(self, cell):
        manager = self._ensure_chunk_upload_manager()
        before_signature = self._resident_chunk_signature(manager, cell)
        manager.set_frame_limits(
            operations_per_chunk=getattr(
                self,
                "_current_upload_operations_per_chunk",
                getattr(self, "_upload_groups_per_frame", 1),
            ),
            time_budget_ms=getattr(
                self,
                "_current_upload_time_budget_ms",
                getattr(self, "_upload_time_budget_ms", 3.0),
            ),
            timing=getattr(self, "_streaming_frame_timing", None),
        )
        try:
            manager.on_chunk_unload(cell)
        finally:
            if before_signature != self._resident_chunk_signature(manager, cell):
                self._invalidate_visible_chunk_cache()
            self._sync_chunk_upload_state_from_manager(manager)

    def _apply_shading_toggle_to_cell(self, cell) -> None:
        manager = self._ensure_chunk_upload_manager()
        manager.apply_shading_toggle_to_cell(cell)
        self._sync_chunk_upload_state_from_manager(manager)

    def _apply_shading_toggle(self) -> None:
        """Rewrite loaded VBO normals through the chunk upload manager."""
        manager = self._ensure_chunk_upload_manager()
        manager.apply_shading_toggle(world=getattr(self, "world", None))
        self._sync_chunk_upload_state_from_manager(manager)

    def _buttons_locked_for_loading(self) -> bool:
        """True while map loading should disable the right-side button block."""
        if not self._has_map_loaded:
            return True
        if not self._initial_chunks_loaded:
            return True
        # Once the initial chunks are resident, release the loading-time render
        # mode lock while the startup help screen is still covering the view.
        # That lets Texture turn back on and gives the renderer real textured
        # frames to settle before the user dismisses the overlay.
        return False

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

    def _reset_initial_chunk_loading_state(self) -> None:
        """Reset ordinary map-load readiness before streaming a new map."""
        self._streaming_runtime.reset()
        self._chunk_prep_progress = 0.0
        self._chunk_prep_complete_until = None
        self._chunk_prep_completion_armed = False
        manifest = getattr(self, "manifest", {})
        source_obj = (
            manifest.get("source_obj", "map")
            if isinstance(manifest, Mapping)
            else "map"
        )
        map_name = os.path.basename(str(source_obj or "map"))
        self._ensure_map_opening_progress_session().begin_streaming(map_name)

    def _startup_visual_prefetch_is_active(self) -> bool:
        overlay = getattr(self, "controls_overlay", None)
        return (
            overlay is not None
            and bool(getattr(overlay, "is_waiting_for_begin", False))
            and not getattr(self, "_initial_visual_ready", False)
        )

    def _target_streaming_load_radius(self) -> int:
        base_radius = max(1, int(self.render_distance_stepper.value))
        return viewer_streaming_runtime.target_load_radius(
            base_radius,
            startup_active=self._startup_visual_prefetch_is_active(),
            maximum_radius=int(
                getattr(
                    self.render_distance_stepper,
                    "max_value",
                    self._STARTUP_VISUAL_RADIUS_MAX_CHUNKS,
                )
            ),
            startup_extra=self._STARTUP_VISUAL_RADIUS_EXTRA_CHUNKS,
            startup_maximum=self._STARTUP_VISUAL_RADIUS_MAX_CHUNKS,
        )

    def _streaming_cell_priority_key(
        self,
    ) -> Callable[[tuple[int, int, int]], tuple[int, int, float, float, float]]:
        world = getattr(self, "world", None)
        world_config = getattr(world, "config", None)
        wnd = getattr(self, "wnd", None)
        window_size = getattr(wnd, "size", _DEFAULT_WINDOW_SIZE)
        return viewer_streaming_runtime.streaming_cell_priority_key(
            camera_position=self.camera.position,
            camera_forward=self.camera.forward(),
            chunk_size=float(getattr(world_config, "chunk_size", 1.0)),
            window_size=window_size,
            fov_deg=float(getattr(self.camera, "fov_deg", 75.0)),
        )

    def _startup_upload_boost_is_active(self) -> bool:
        overlay = getattr(self, "controls_overlay", None)
        return (
            overlay is not None
            and overlay.is_waiting_for_begin
            and not getattr(self, "_initial_chunks_loaded", False)
        )

    def _streaming_upload_limits(self, stats: dict | None = None) -> tuple[int, int, float]:
        """Return chunk/operation/time upload limits for the current frame."""
        policy = viewer_streaming_runtime.UploadBudgetPolicy(
            normal=viewer_streaming_runtime.UploadLimits(
                self._upload_chunks_per_frame,
                self._upload_groups_per_frame,
                self._upload_time_budget_ms,
            ),
            catchup=viewer_streaming_runtime.UploadLimits(
                _CATCHUP_UPLOAD_CHUNKS_PER_FRAME,
                _CATCHUP_UPLOAD_OPERATIONS_PER_CHUNK,
                _CATCHUP_UPLOAD_TIME_BUDGET_MS,
            ),
            startup=viewer_streaming_runtime.UploadLimits(
                _STARTUP_UPLOAD_CHUNKS_PER_FRAME,
                _STARTUP_UPLOAD_OPERATIONS_PER_CHUNK,
                _STARTUP_UPLOAD_TIME_BUDGET_MS,
            ),
        )
        limits = policy.limits_for(
            stats,
            startup_active=self._startup_upload_boost_is_active(),
        )
        return limits.chunks, limits.operations_per_chunk, limits.time_budget_ms

    @staticmethod
    def _initial_chunk_load_needed(
        stats: dict,
        max_loaded_chunks: int,
    ) -> int:
        return viewer_streaming_runtime.initial_chunk_load_needed(
            stats,
            max_loaded_chunks,
            minimum_chunks=CaveViewerWindow._INITIAL_LOAD_MIN_CHUNKS,
        )

    def _initial_chunk_load_is_ready(self, stats: dict) -> bool:
        max_loaded = max(1, int(getattr(self.world.config, "max_loaded_chunks", self._INITIAL_LOAD_MIN_CHUNKS)))
        return viewer_streaming_runtime.initial_chunk_load_is_ready(
            stats,
            max_loaded,
            minimum_chunks=self._INITIAL_LOAD_MIN_CHUNKS,
        )

    @staticmethod
    def _texture_source_key(source: object) -> object:
        return viewer_streaming_runtime.texture_source_key(source)

    def _current_wanted_cells_snapshot(self) -> frozenset[tuple[int, int, int]]:
        world = getattr(self, "world", None)
        snapshot = getattr(world, "wanted_cells_snapshot", None)
        if callable(snapshot):
            return frozenset(snapshot())
        return frozenset(getattr(world, "_last_wanted_cells", ()))

    def _texture_sources_for_cells(
        self,
        cells: Iterable[tuple[int, int, int]],
    ) -> set[object]:
        texture_manager = getattr(self, "texture_manager", None)
        material_to_file = getattr(texture_manager, "material_to_file", {})
        if not isinstance(material_to_file, Mapping):
            return set()
        manifest = getattr(self, "manifest", {})
        if not isinstance(manifest, Mapping):
            return set()
        return viewer_streaming_runtime.texture_sources_for_cells(
            cells,
            manifest=manifest,
            material_to_file=material_to_file,
        )

    def _texture_sources_for_visible_cells(
        self,
        visible_cells: Iterable[tuple[tuple, list]] | None,
    ) -> set[object]:
        texture_manager = getattr(self, "texture_manager", None)
        material_to_file = getattr(texture_manager, "material_to_file", {})
        if visible_cells is None or not isinstance(material_to_file, Mapping):
            return set()
        return viewer_streaming_runtime.texture_sources_for_visible_cells(
            visible_cells,
            material_to_file=material_to_file,
        )

    def _resident_texture_source_keys(self) -> tuple[set[object], bool]:
        texture_manager = getattr(self, "texture_manager", None)
        resident_sources = None
        known_exact = False
        if texture_manager is not None:
            exact_sources = getattr(texture_manager, "resident_texture_sources", None)
            if callable(exact_sources):
                resident_sources = exact_sources()
                known_exact = True
        if resident_sources is None:
            return set(), known_exact
        return {
            self._texture_source_key(source)
            for source in resident_sources
            if source
        }, known_exact

    def _benchmark_route_prefetch_stats(self) -> dict[str, object]:
        prefetch_cells = frozenset(
            getattr(self, "_benchmark_route_prefetch_cells", ())
        )
        world = getattr(self, "world", None)
        if not prefetch_cells or world is None:
            return viewer_streaming_runtime.route_prefetch_stats(
                prefetch_cells,
                world_available=world is not None,
            ).as_dict()
        lock = getattr(world, "_lock", None)
        if lock is None:
            loaded_cells = frozenset(getattr(world, "loaded_cells", set()))
            pending_cells = frozenset(getattr(world, "_pending", set()))
            failed_cells = frozenset(getattr(world, "_failed_cells", {}))
        else:
            with lock:
                loaded_cells = frozenset(getattr(world, "loaded_cells", set()))
                pending_cells = frozenset(getattr(world, "_pending", set()))
                failed_cells = frozenset(getattr(world, "_failed_cells", {}))
        return viewer_streaming_runtime.route_prefetch_stats(
            prefetch_cells,
            loaded_cells=loaded_cells,
            pending_cells=pending_cells,
            failed_cells=failed_cells,
        ).as_dict()

    def _initial_texture_readiness_stats(
        self,
        visible_cells: Iterable[tuple[tuple, list]] | None,
    ) -> dict[str, object]:
        texture_manager = getattr(self, "texture_manager", None)
        manager_stats = (
            texture_manager.stats()
            if texture_manager is not None and hasattr(texture_manager, "stats")
            else {}
        )
        wanted_sources = self._texture_sources_for_cells(
            self._current_wanted_cells_snapshot()
        )
        visible_sources = self._texture_sources_for_visible_cells(visible_cells)
        resident_sources, exact_sources_known = self._resident_texture_source_keys()
        return viewer_streaming_runtime.texture_readiness(
            wanted_sources=wanted_sources,
            visible_sources=visible_sources,
            resident_sources=resident_sources,
            exact_sources_known=exact_sources_known,
            manager_stats=manager_stats,
        ).as_dict()

    def _manifest_chunk_bounds(
        self,
        cell: tuple[int, int, int],
    ) -> tuple[np.ndarray, np.ndarray] | None:
        manifest = getattr(self, "manifest", {})
        if not isinstance(manifest, Mapping):
            return None
        return viewer_streaming_runtime.manifest_chunk_bounds(manifest, cell)

    def _failed_cells_snapshot(self) -> frozenset[tuple[int, int, int]]:
        world = getattr(self, "world", None)
        failed_cells = getattr(world, "_failed_cells", {})
        if isinstance(failed_cells, Mapping):
            return frozenset(failed_cells.keys())
        return frozenset(failed_cells or ())

    def _startup_visual_coverage_stats(
        self,
        visible_cells: Iterable[tuple[tuple, list]] | None,
        view: np.ndarray | None,
        projection: np.ndarray | None,
    ) -> dict[str, object]:
        wanted_cells = self._current_wanted_cells_snapshot()
        manifest = getattr(self, "manifest", {})
        if not isinstance(manifest, Mapping):
            manifest = {}
        return viewer_streaming_runtime.startup_visual_coverage(
            wanted_cells=wanted_cells,
            visible_cells=visible_cells,
            failed_cells=self._failed_cells_snapshot(),
            manifest=manifest,
            view=view,
            projection=projection,
        ).as_dict()

    def _initial_visual_readiness_stats(
        self,
        stats: dict,
        visible_chunk_count: int,
        visible_cells: Iterable[tuple[tuple, list]] | None = None,
        view: np.ndarray | None = None,
        projection: np.ndarray | None = None,
    ) -> dict:
        """Advance startup readiness from immutable per-frame snapshots."""
        texture_data = self._initial_texture_readiness_stats(visible_cells)
        coverage_data = self._startup_visual_coverage_stats(
            visible_cells,
            view,
            projection,
        )
        route_data = self._benchmark_route_prefetch_stats()
        max_loaded = max(
            1,
            int(
                getattr(
                    self.world.config,
                    "max_loaded_chunks",
                    self._INITIAL_LOAD_MIN_CHUNKS,
                )
            ),
        )
        visual_stats, became_ready = self._streaming_runtime.observe_visual_readiness(
            stats,
            visible_chunk_count=visible_chunk_count,
            max_loaded_chunks=max_loaded,
            upload_state_count=len(getattr(self, "_chunk_upload_states", {})),
            textures=viewer_streaming_runtime.TextureReadiness(**texture_data),
            coverage=viewer_streaming_runtime.CoverageStats(**coverage_data),
            route=viewer_streaming_runtime.RoutePrefetchStats(**route_data),
            settle_frames=self._INITIAL_VISUAL_READY_SETTLE_FRAMES,
        )
        if became_ready:
            self._log_initial_visual_ready_complete(
                stats,
                visible_chunk_count=visible_chunk_count,
            )
        return visual_stats

    def _log_initial_visual_ready_complete(
        self,
        stats: dict,
        *,
        visible_chunk_count: int,
    ) -> None:
        if getattr(self, "_initial_visual_ready_logged", False):
            return
        self._initial_visual_ready_logged = True
        started_at = getattr(self, "_initial_compilation_started_at", None)
        elapsed_s = (
            0.0
            if started_at is None
            else max(0.0, time.perf_counter() - started_at)
        )
        upload_states = getattr(self, "_chunk_upload_states", {})
        required_textures = int(
            getattr(self, "_initial_visual_ready_required_textures", 0)
        )
        resident_textures = int(
            getattr(self, "_initial_visual_ready_resident_textures", 0)
        )
        visible_textures = int(
            getattr(self, "_initial_visual_ready_visible_textures", 0)
        )
        missing_textures = int(
            getattr(self, "_initial_visual_ready_missing_textures", 0)
        )
        expected_chunks = int(
            getattr(self, "_initial_visual_ready_expected_chunks", 0)
        )
        covered_chunks = int(
            getattr(self, "_initial_visual_ready_covered_chunks", 0)
        )
        missing_chunks = int(
            getattr(self, "_initial_visual_ready_missing_chunks", 0)
        )
        coverage_pct = float(
            getattr(self, "_initial_visual_ready_coverage_pct", 100.0)
        )
        route_prefetch_expected = int(
            getattr(self, "_initial_route_prefetch_expected_cells", 0)
        )
        route_prefetch_loaded = int(
            getattr(self, "_initial_route_prefetch_loaded_cells", 0)
        )
        route_prefetch_pending = int(
            getattr(self, "_initial_route_prefetch_pending_cells", 0)
        )
        route_prefetch_failed = int(
            getattr(self, "_initial_route_prefetch_failed_cells", 0)
        )
        route_prefetch_missing = int(
            getattr(self, "_initial_route_prefetch_missing_cells", 0)
        )
        route_prefetch_coverage_pct = float(
            getattr(self, "_initial_route_prefetch_coverage_pct", 100.0)
        )
        world = getattr(self, "world", None)
        startup_radius = int(
            getattr(getattr(world, "config", None), "load_radius_cells", 0) or 0
        )
        _LOG.info(
            "Initial visual readiness completed in %.2fs "
            "(visible=%d loaded=%d pending=%d ready=%d wanted=%d "
            "upload_states=%d textures=%d/%d missing_textures=%d "
            "visible_textures=%d coverage=%d/%d missing_chunks=%d %.1f%% "
            "startup_radius=%d route_prefetch=%d/%d pending=%d failed=%d "
            "missing=%d %.1f%%).",
            elapsed_s,
            int(visible_chunk_count),
            int(stats.get("loaded", 0)),
            int(stats.get("pending", 0)),
            int(stats.get("ready", 0)),
            int(stats.get("wanted", 0)),
            len(upload_states),
            resident_textures,
            required_textures,
            missing_textures,
            visible_textures,
            covered_chunks,
            expected_chunks,
            missing_chunks,
            coverage_pct,
            startup_radius,
            route_prefetch_loaded,
            route_prefetch_expected,
            route_prefetch_pending,
            route_prefetch_failed,
            route_prefetch_missing,
            route_prefetch_coverage_pct,
        )
        benchmark_controller = self._active_benchmark_controller()
        if benchmark_controller is not None:
            benchmark_controller.update_environment(
                {
                    "initial_visual_ready_seconds": round(elapsed_s, 6),
                    "initial_visual_ready_visible_chunks": int(visible_chunk_count),
                    "initial_visual_ready_frames": int(
                        getattr(self, "_initial_visual_ready_frames", 0)
                    ),
                    "initial_visual_ready_required_textures": required_textures,
                    "initial_visual_ready_resident_textures": resident_textures,
                    "initial_visual_ready_visible_textures": visible_textures,
                    "initial_visual_ready_missing_textures": missing_textures,
                    "initial_visual_ready_expected_chunks": expected_chunks,
                    "initial_visual_ready_covered_chunks": covered_chunks,
                    "initial_visual_ready_missing_chunks": missing_chunks,
                    "initial_visual_ready_coverage_pct": round(coverage_pct, 3),
                    "initial_visual_ready_load_radius_chunks": startup_radius,
                    "initial_route_prefetch_expected_cells": route_prefetch_expected,
                    "initial_route_prefetch_loaded_cells": route_prefetch_loaded,
                    "initial_route_prefetch_pending_cells": route_prefetch_pending,
                    "initial_route_prefetch_failed_cells": route_prefetch_failed,
                    "initial_route_prefetch_missing_cells": route_prefetch_missing,
                    "initial_route_prefetch_coverage_pct": round(
                        route_prefetch_coverage_pct,
                        3,
                    ),
                }
            )

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

    def _log_main_thread_stall(
        self,
        label: str,
        elapsed_s: float,
        **details: object,
    ) -> None:
        if elapsed_s < _MAIN_THREAD_STALL_LOG_THRESHOLD_S:
            return
        last_logs = getattr(self, "_main_thread_stall_last_log_at", None)
        if last_logs is None:
            last_logs = {}
            self._main_thread_stall_last_log_at = last_logs
        now = time.perf_counter()
        last_logged_at = last_logs.get(label)
        if (
            last_logged_at is not None
            and now - last_logged_at < _MAIN_THREAD_STALL_LOG_MIN_INTERVAL_S
        ):
            return
        last_logs[label] = now
        detail_items = [
            f"{name}={value}"
            for name, value in details.items()
            if value is not None
        ]
        detail_text = f" ({' '.join(detail_items)})" if detail_items else ""
        _LOG.warning(
            "Main-thread stall: %s took %.0fms%s.",
            label,
            elapsed_s * 1000.0,
            detail_text,
        )

    def _initial_chunk_load_progress(self, stats: dict) -> float:
        max_loaded = max(1, int(getattr(self.world.config, "max_loaded_chunks", self._INITIAL_LOAD_MIN_CHUNKS)))
        return viewer_streaming_runtime.initial_chunk_load_progress(
            stats,
            max_loaded,
            minimum_chunks=self._INITIAL_LOAD_MIN_CHUNKS,
        )

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
        return view_culling.frustum_planes(view, proj)

    @staticmethod
    def _aabb_inside_frustum(planes: np.ndarray,
                              bmin: np.ndarray, bmax: np.ndarray) -> bool:
        return view_culling.aabb_inside_frustum(planes, bmin, bmax)
