"""Render-thread frame and HUD presentation integration for the viewer window."""

from __future__ import annotations

import logging
import math
import os
import time

import moderngl
import numpy as np

from caveviewer.benchmarking.results import BenchmarkController
from caveviewer.core.diagnostics.logging import get_logger
from caveviewer.core.diagnostics.runtime import record_runtime_stage
from caveviewer.gui import bitmap_font, viewer_scene_presentation
from caveviewer.gui.render_mode_buttons import RenderModeButtons
from caveviewer.gui.stepper_control import StepperControl
from caveviewer.gui.viewer_capture_workflow import CaptureOverlayMode, CaptureOverlayState
from caveviewer.gui.viewer_frame_scheduler import ViewerFramePhase, ViewerFrameState
from caveviewer.gui.viewer_window_sizing import (
    viewer_ui_scale_for_window_size as _viewer_ui_scale_for_window_size,
    viewer_ui_surface_size as _viewer_ui_surface_size,
    window_pixel_ratio as _window_pixel_ratio,
)

_LOG = get_logger("CaveViewer")
_ICONIFIED_RENDER_POLL_INTERVAL_S = 0.12
_IMPORT_PAUSE_NOTICE_RENDER_INTERVAL_S = 1.0 / 30.0

class ViewerWindowPresentationIntegration:
    """Methods executed by ``CaveViewerWindow`` on its owning callback thread."""

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
        viewer_settings = getattr(self, "_viewer_runtime_settings", None)
        viewer_ui_scale = _viewer_ui_scale_for_window_size(
            _viewer_ui_surface_size(getattr(self, "wnd", None), window_size),
            environ={} if viewer_settings is not None else None,
            configured_scale=(
                viewer_settings.viewer_ui_scale
                if viewer_settings is not None
                else None
            ),
        )
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
        above it (see StepperControl.render's label_above handling).

        Stack order, top to bottom: Brightness, Global Light, Render
        Distance, then the button block. The panel is the shared horizontal
        layout container: labels, steppers, and buttons all use its content
        center so a wide label cannot make the controls appear right-aligned
        within the backplate.
        """
        self._update_right_column_hud_scale(window_size)
        if window_size == self._layout_cache_size:
            return self._layout_cache_result

        _width, h = window_size

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
        label_height = bitmap_font.text_height_px(fixed_label_size)
        label_widths = (
            bitmap_font.text_width_px(self.light_stepper.label, fixed_label_size),
            bitmap_font.text_width_px(self.ambient_stepper.label, fixed_label_size),
            bitmap_font.text_width_px(
                self.render_distance_stepper.label,
                fixed_label_size,
            ),
        )
        stepper_widths = (
            self.light_stepper.total_width(),
            self.ambient_stepper.total_width(),
            self.render_distance_stepper.total_width(),
        )

        button_block_height = RenderModeButtons.total_stack_height(scale=panel_scale)
        content_bottom_inset = (
            self.RIGHT_COLUMN_PANEL_BOTTOM_MARGIN + self.RIGHT_COLUMN_PANEL_BOTTOM_PAD
        ) * viewer_ui_scale
        buttons_bottom_y = h - content_bottom_inset
        buttons_top_y = buttons_bottom_y - button_block_height
        button_layout = self.render_mode_buttons._group_layout(
            window_size,
            buttons_top_y,
        )
        button_width = RenderModeButtons.BUTTON_WIDTH * button_layout["scale"]
        result = viewer_scene_presentation.right_column_layout(
            viewer_scene_presentation.RightColumnLayoutInput(
                window_size=window_size,
                stepper_widths=stepper_widths,
                stepper_heights=(
                    self.light_stepper.total_height(),
                    self.ambient_stepper.total_height(),
                    self.render_distance_stepper.total_height(),
                ),
                label_widths=label_widths,
                label_height=label_height,
                button_width=button_width,
                button_height=button_block_height,
                panel_scale=panel_scale,
                ui_scale=viewer_ui_scale,
                bottom_margin=self.RIGHT_COLUMN_PANEL_BOTTOM_MARGIN,
                bottom_padding=self.RIGHT_COLUMN_PANEL_BOTTOM_PAD,
                right_margin=self.RIGHT_COLUMN_PANEL_RIGHT_MARGIN,
                side_padding=self.RIGHT_COLUMN_PANEL_SIDE_PAD,
                top_padding=self.RIGHT_COLUMN_PANEL_TOP_PAD,
                label_gap=self.RIGHT_COLUMN_PANEL_LABEL_GAP,
                column_gap=self.RIGHT_COLUMN_GAP,
                button_group_gap=self.RIGHT_COLUMN_BUTTON_GROUP_GAP,
            )
        ).as_dict()
        self._layout_cache_size = window_size
        self._layout_cache_result = result
        return result

    def _right_column_panel_rect(self, window_size: tuple[int, int], column: dict | None = None) -> tuple[float, float, float, float]:
        """Bounds for the shared backplate behind the right-side HUD column."""
        if column is None:
            column = self._right_column_layout(window_size)
        return column["panel_rect"]

    def _render_right_column_panel(self, window_size: tuple[int, int], column: dict | None = None) -> None:
        """Draw a shared translucent panel behind the right-side HUD controls."""
        if column is None:
            column = self._right_column_layout(window_size)
        self._ensure_scene_presentation().render_right_column_panel(
            window_size=window_size,
            panel_rect=self._right_column_panel_rect(window_size, column),
            fill_rgba=self.RIGHT_COLUMN_PANEL_FILL_RGBA,
            border_px=self.RIGHT_COLUMN_PANEL_BORDER_PX,
            border_rgba=self.RIGHT_COLUMN_PANEL_BORDER_RGBA,
        )

    def _render_minimap(self, window_size: tuple[int, int]) -> None:
        """Draw the minimap in the normal HUD, keeping it out of recordings."""
        if self.minimap is not None:
            self.minimap.render(window_size, self.camera.position, self.camera.forward(),
                                self._bookmarks)

    def _render_capture_status_message(self, window_size: tuple[int, int]) -> None:
        now = time.perf_counter()
        status = self._ensure_recording_controller().active_status(now=now)
        if status is None:
            return

        message = status.message
        detail = status.detail
        kind = status.kind or "info"

        w, h = window_size
        self._render_recording_countdown_scrim(window_size, alpha=0.42)

        symbol = {
            "success": "OK",
            "error": "!",
            "cancel": "X",
            "info": "...",
        }.get(kind, "...")
        symbol_size = 5.2 if symbol == "OK" else 3.8 if symbol == "..." else 7.2
        center_x = w / 2.0
        ring_center_y = h / 2.0
        self.import_progress_panel.draw_circle_label(
            center_x=center_x,
            center_y=ring_center_y,
            window_size=window_size,
            label=symbol,
            progress=None if kind == "info" else 1.0,
            pixel_size=symbol_size,
            fixed_text_scale=self.UI_TEXT_SCALE,
            stage=message,
            note=detail,
        )
        self._ensure_capture_workflow().mark_exit_status_presented(
            now=time.perf_counter()
        )

    def _render_recording_countdown_scrim(self, window_size: tuple[int, int], alpha: float = 0.62) -> None:
        """Darken the cave view behind the countdown ring without hiding it."""
        self._ensure_scene_presentation().render_scrim(
            window_size,
            alpha=alpha,
        )

    def _frame_phase(self) -> ViewerFramePhase:
        """Select this callback's non-GL session phase."""
        setup_complete = bool(getattr(self, "_window_setup_complete", False))
        closing_requested = bool(getattr(self, "_closing_requested", False))
        if not setup_complete or closing_requested:
            return ViewerFramePhase.INACTIVE
        request = self._workflow_render_request()
        if request is not None:
            return request.phase
        return self._ensure_frame_scheduler().phase_for(
            ViewerFrameState(
                setup_complete=setup_complete,
                closing_requested=closing_requested,
                iconified=bool(getattr(self, "_is_iconified", False)),
                finalizing_capture=self._capture_close_pending(),
                import_active=bool(getattr(self, "_import_active", False)),
                map_loaded=bool(getattr(self, "_has_map_loaded", False)),
            )
        )

    def _update_interactive_input(
        self,
        frame_time: float,
        *,
        benchmark_controller: BenchmarkController | None,
        benchmark_active: bool,
    ) -> float:
        """Apply navigation and workflow updates before streaming begins."""
        if frame_time > 2.0:
            self._reset_transient_input_state("long frame gap")

        started = time.perf_counter()
        dt = max(frame_time, 1e-4)
        if benchmark_active:
            if benchmark_controller.started:
                benchmark_controller.update_camera(self.camera, time.perf_counter())
        elif self._recorded_dive_is_active():
            if self._recorded_dive_is_paused():
                self._handle_paused_recorded_dive_input(dt)
                self._update_recorded_dive(now=time.perf_counter())
            elif self._continuous_input_has_navigation_intent(dt):
                self._stop_recorded_dive(reason="manual_control")
                self._handle_continuous_input(dt)
            else:
                self._update_recorded_dive(now=time.perf_counter())
        else:
            self._handle_manual_input_frame(dt, now=time.perf_counter())
        self._update_manual_dive_trace()
        if self._slice_work_pending():
            self._update_slice_export()
        return (time.perf_counter() - started) * 1000.0

    def _advance_interactive_streaming(self) -> tuple[float, dict, dict]:
        """Advance render-thread world and upload work for one frame."""
        target_load_radius = self._target_streaming_load_radius()
        if self.world.config.load_radius_cells != target_load_radius:
            self.world.config.load_radius_cells = target_load_radius

        started = time.perf_counter()
        streaming_timing = self._new_streaming_frame_timing()
        self._streaming_frame_timing = streaming_timing
        try:
            update_started = time.perf_counter()
            self.world.update(
                self.camera.position.astype(np.float32),
                cell_priority_key=self._streaming_cell_priority_key(),
            )
            update_elapsed_s = time.perf_counter() - update_started
            streaming_timing["update_ms"] = update_elapsed_s * 1000.0
            self._log_main_thread_stall("streaming update", update_elapsed_s)

            pre_drain_stats = self.world.stats()
            (
                upload_chunks_per_frame,
                upload_operations_per_chunk,
                upload_time_budget_ms,
            ) = self._streaming_upload_limits(pre_drain_stats)
            self._current_upload_operations_per_chunk = upload_operations_per_chunk
            self._current_upload_time_budget_ms = upload_time_budget_ms
            drain_started = time.perf_counter()
            ready_drain_started = time.perf_counter()
            self.world.drain_ready_chunks(
                self._on_chunk_ready,
                self._on_chunk_unload,
                max_per_frame=upload_chunks_per_frame,
                time_budget_ms=upload_time_budget_ms,
            )
            ready_drain_elapsed_s = time.perf_counter() - ready_drain_started
            streaming_timing["ready_drain_ms"] = ready_drain_elapsed_s * 1000.0
            self._log_main_thread_stall(
                "ready chunk drain",
                ready_drain_elapsed_s,
                ready=int(pre_drain_stats.get("ready", 0)),
                pending=int(pre_drain_stats.get("pending", 0)),
                max_per_frame=upload_chunks_per_frame,
                time_budget_ms=upload_time_budget_ms,
            )
            failure_drain_started = time.perf_counter()
            self._drain_streaming_worker_failures()
            streaming_timing["failure_drain_ms"] = (
                time.perf_counter() - failure_drain_started
            ) * 1000.0
            streaming_timing["drain_ms"] = (
                time.perf_counter() - drain_started
            ) * 1000.0
            self._record_upload_slice_sizes(streaming_timing)
        finally:
            upload_manager = getattr(self, "_chunk_upload_manager", None)
            if upload_manager is not None:
                upload_manager.clear_frame_timing()
            self._streaming_frame_timing = None
        return (
            (time.perf_counter() - started) * 1000.0,
            streaming_timing,
            self.world.stats(),
        )

    def _render_initial_streaming_state(
        self,
        stats: dict,
        *,
        benchmark_controller: BenchmarkController | None,
        benchmark_active: bool,
    ) -> bool:
        """Render startup progress when the map is not ready for interaction."""
        if not self._initial_chunks_loaded and self._initial_chunk_load_is_ready(stats):
            self._initial_chunks_loaded = True
            self._log_initial_compilation_complete(stats)

        if self._initial_chunks_loaded and not self._chunk_prep_completion_armed:
            self._chunk_prep_completion_armed = True
            self._chunk_prep_complete_until = (
                time.perf_counter() + self._CHUNK_PREP_COMPLETE_HOLD_SECONDS
            )

        now = time.perf_counter()
        if not self._initial_chunks_loaded:
            if benchmark_active and benchmark_controller.exceeded_max_runtime(now):
                self._finish_benchmark(reason="max_runtime_exceeded")
                self.close()
                return True
            map_name = os.path.basename(self.manifest.get("source_obj", "map"))
            raw_fraction = self._initial_chunk_load_progress(stats)
            target = min(
                self._CHUNK_PREP_MAX_FRACTION,
                raw_fraction * self._CHUNK_PREP_MAX_FRACTION,
            )
            self._chunk_prep_progress = max(self._chunk_prep_progress, target)
            frame = self._ensure_map_opening_progress_session().observe_streaming(
                map_name,
                self._chunk_prep_progress,
            )
            self._render_map_opening_progress(frame)
            return True

        if (
            self._chunk_prep_complete_until is not None
            and now < self._chunk_prep_complete_until
        ):
            if benchmark_active and benchmark_controller.exceeded_max_runtime(now):
                self._finish_benchmark(reason="max_runtime_exceeded")
                self.close()
                return True
            map_name = os.path.basename(self.manifest.get("source_obj", "map"))
            frame = self._ensure_map_opening_progress_session().complete(map_name)
            self._render_map_opening_progress(frame)
            return True

        self._chunk_prep_complete_until = None
        self._ensure_map_opening_progress_session().finish()
        if benchmark_active:
            self._sync_render_mode_loading_policy()
        return False

    def _render_interactive_scene(
        self,
        stats: dict,
    ) -> viewer_scene_presentation.PresentedScene:
        """Cull and draw the cave scene, returning facts for later stages."""
        aspect = self.wnd.size[0] / max(self.wnd.size[1], 1)
        view = self.camera.view_matrix()
        projection = self.camera.projection_matrix(aspect)
        ambient_t = self.ambient_stepper.value / self.ambient_stepper.max_value
        ambient_value = self._AMBIENT_MIN + ambient_t * (
            self._AMBIENT_MAX - self._AMBIENT_MIN
        )
        presentation = self._ensure_scene_presentation()
        setup_ms = presentation.configure_scene(
            background=tuple(self.color_picker.color),
            view=view,
            projection=projection,
            camera_position=self.camera.position,
            light_intensity=float(self.light_stepper.value),
            ambient=ambient_value,
            texture_enabled=self.render_mode_buttons.texture_enabled,
        )

        cull_started = time.perf_counter()
        visible_cells = self._visible_chunk_gpu_objects(view, projection)
        chunks_drawn = len(visible_cells)
        cull_ms = (time.perf_counter() - cull_started) * 1000.0
        visual_stats = self._initial_visual_readiness_stats(
            stats,
            chunks_drawn,
            visible_cells=visible_cells,
            view=view,
            projection=projection,
        )
        draw_request = viewer_scene_presentation.SceneDrawRequest(
            visible_cells=tuple(visible_cells),
            solid_enabled=(
                self.render_mode_buttons.texture_enabled
                or not self.render_mode_buttons.wireframe_enabled
            ),
            wireframe_enabled=self.render_mode_buttons.wireframe_enabled,
        )
        draw_timing = presentation.draw_mesh(
            draw_request,
            gpu_timer_enabled=self._gpu_draw_timer_enabled,
        )
        self._last_gpu_draw_ms = draw_timing.gpu_draw_ms
        return viewer_scene_presentation.PresentedScene(
            view=view,
            projection=projection,
            draw_request=draw_request,
            visual_stats=visual_stats,
            chunks_drawn=chunks_drawn,
            setup_ms=setup_ms,
            cull_ms=cull_ms,
            draw_ms=draw_timing.draw_ms,
            submit_ms=draw_timing.submit_ms,
            gpu_query_wait_ms=draw_timing.gpu_query_wait_ms,
        )

    def _render_recording_scene_frame(
        self,
        scene: viewer_scene_presentation.PresentedScene,
        framebuffer: moderngl.Framebuffer,
        output_size: tuple[int, int],
    ) -> None:
        """Redraw the current scene into the recording framebuffer."""
        output_width, output_height = output_size
        recording_projection = self.camera.projection_matrix(
            output_width / max(output_height, 1)
        )
        self._ensure_scene_presentation().render_recording_frame(
            framebuffer=framebuffer,
            output_size=output_size,
            background=tuple(self.color_picker.color),
            view=scene.view,
            screen_projection=scene.projection,
            recording_projection=recording_projection,
            request=scene.draw_request,
        )

    def _render_interactive_overlays(
        self,
        scene: viewer_scene_presentation.PresentedScene,
    ) -> viewer_scene_presentation.OverlayTimings:
        """Draw capture presentation or the ordinary HUD over the scene."""
        recording_read_ms = 0.0
        recording_stage_ms = 0.0
        recording_drain_ms = 0.0
        workflow_request = self._workflow_render_request()
        capture_overlay_mode = (
            workflow_request.capture_overlay_mode
            if workflow_request is not None
            else self._ensure_capture_workflow().overlay_mode_for(
                CaptureOverlayState(
                    recording_armed=self._recording_hides_hud(),
                    manual_dive_trace_countdown_active=(
                        self._ensure_manual_dive_trace_controller().countdown_active
                    ),
                    slice_countdown_active=(
                        self._ensure_slice_selection_controller().countdown_active
                    ),
                )
            )
        )
        if capture_overlay_mode is CaptureOverlayMode.RECORDING:
            now = time.perf_counter()
            if (
                self._recording_countdown_until is not None
                and now < self._recording_countdown_until
            ):
                self._render_countdown_overlay(
                    now=now,
                    controller=self._ensure_recording_controller(),
                    start_number=self.RECORDING_COUNTDOWN_START_NUMBER,
                    title=self.RECORDING_COUNTDOWN_TITLE,
                    note=self._countdown_cancel_note("R"),
                )
            else:
                recording_read_ms = self._recording_update_after_scene(
                    now,
                    render_frame=lambda framebuffer, output_size: (
                        self._render_recording_scene_frame(
                            scene,
                            framebuffer,
                            output_size,
                        )
                    ),
                )
                recording_stage_ms = self._recording_last_stage_ms
                recording_drain_ms = self._recording_last_drain_ms
                self._render_capture_status_message(self.wnd.size)
            overlay_ms = 0.0
        elif capture_overlay_mode is CaptureOverlayMode.MANUAL_DIVE_TRACE_COUNTDOWN:
            now = time.perf_counter()
            self._render_countdown_overlay(
                now=now,
                controller=self._ensure_manual_dive_trace_controller(),
                start_number=self.MANUAL_DIVE_TRACE_COUNTDOWN_START_NUMBER,
                title=self.MANUAL_DIVE_TRACE_COUNTDOWN_TITLE,
                note=self._countdown_cancel_note("T"),
            )
            overlay_ms = 0.0
        elif capture_overlay_mode is CaptureOverlayMode.SLICE_COUNTDOWN:
            now = time.perf_counter()
            self._render_countdown_overlay(
                now=now,
                controller=self._ensure_slice_selection_controller(),
                start_number=self.SLICE_COUNTDOWN_START_NUMBER,
                title=self.SLICE_COUNTDOWN_TITLE,
                note=self._countdown_cancel_note("C"),
            )
            overlay_ms = 0.0
        else:
            overlay_started = time.perf_counter()
            column = self._right_column_layout(self.wnd.size)
            brightness_anchor_x, brightness_anchor_y = column["brightness_anchor"]
            ambient_anchor_x, ambient_anchor_y = column["ambient_anchor"]
            distance_anchor_x, distance_anchor_y = column[
                "render_distance_anchor"
            ]
            buttons_top_y = column["buttons_top_y"]

            self._render_right_column_panel(self.wnd.size, column)
            self.light_stepper.render(
                self.wnd.size,
                brightness_anchor_x,
                brightness_anchor_y,
                label_above=True,
            )
            self.ambient_stepper.render(
                self.wnd.size,
                ambient_anchor_x,
                ambient_anchor_y,
                label_above=True,
            )
            self.render_distance_stepper.render(
                self.wnd.size,
                distance_anchor_x,
                distance_anchor_y,
                label_above=True,
            )
            self._render_minimap(self.wnd.size)
            self.render_mode_buttons.render(
                self.wnd.size,
                buttons_top_y,
                help_active=self.controls_overlay.is_manual_mode,
                color_active=self.color_picker.is_active,
                right_inset=column["button_right_inset"],
            )
            self.color_picker.render(self.wnd.size)
            self.controls_overlay.update(scene.visual_stats)
            self.controls_overlay.render(self.wnd.size)
            if not self._render_active_capture_instruction(self.wnd.size):
                self._render_dive_status(self.wnd.size)
            self._render_capture_status_message(self.wnd.size)
            overlay_ms = (time.perf_counter() - overlay_started) * 1000.0

        return viewer_scene_presentation.OverlayTimings(
            overlay_ms=overlay_ms,
            recording_read_ms=recording_read_ms,
            recording_stage_ms=recording_stage_ms,
            recording_drain_ms=recording_drain_ms,
        )

    def _update_interactive_benchmark(
        self,
        *,
        benchmark_controller: BenchmarkController,
        scene: viewer_scene_presentation.PresentedScene,
        overlays: viewer_scene_presentation.OverlayTimings,
        total_ms: float,
        streaming_ms: float,
        other_ms: float,
        stats: dict,
        streaming_timing: dict,
    ) -> bool:
        """Record benchmark readiness or frame metrics; return when frame ends."""
        benchmark_now = time.perf_counter()
        if not getattr(self, "_initial_visual_ready", False):
            if benchmark_controller.exceeded_max_runtime(benchmark_now):
                self._finish_benchmark(reason="max_runtime_exceeded")
                self.close()
            return True
        if not benchmark_controller.started:
            self.controls_overlay.dismiss_begin_screen()
            benchmark_controller.update_camera(self.camera, benchmark_now)
            return True
        benchmark_complete = benchmark_controller.record_frame(
            now=benchmark_now,
            frame_ms=total_ms,
            streaming_ms=streaming_ms,
            scene_setup_ms=scene.setup_ms,
            mesh_draw_ms=scene.draw_ms,
            mesh_cull_ms=scene.cull_ms,
            mesh_submit_ms=scene.submit_ms,
            overlay_ms=overlays.overlay_ms,
            other_ms=other_ms,
            drawn_chunks=scene.chunks_drawn,
            resident_chunks=len(self._chunk_gpu_objects),
            world_stats=stats,
            streaming_timing=streaming_timing,
        )
        if benchmark_complete:
            self._finish_benchmark(reason="completed")
            self.close()
            return True
        if benchmark_controller.exceeded_max_runtime(benchmark_now):
            self._finish_benchmark(reason="max_runtime_exceeded")
            self.close()
            return True
        return False

    def _record_interactive_frame_telemetry(
        self,
        *,
        benchmark_controller: BenchmarkController | None,
        benchmark_active: bool,
        scene: viewer_scene_presentation.PresentedScene,
        overlays: viewer_scene_presentation.OverlayTimings,
        total_ms: float,
        input_ms: float,
        streaming_ms: float,
        other_ms: float,
        streaming_timing: dict,
    ) -> None:
        """Publish spike diagnostics and periodic viewer throughput metrics."""
        self._frame_time_history.append(total_ms)
        if len(self._frame_time_history) > 30:
            self._frame_time_history.pop(0)
        rolling_avg = sum(self._frame_time_history) / len(self._frame_time_history)

        if len(self._frame_time_history) >= 10 and total_ms > max(
            rolling_avg * 3,
            25.0,
        ):
            stats = self.world.stats()
            gpu_draw_text = self._format_optional_ms(self._last_gpu_draw_ms)
            _LOG.warning(
                f"FRAME SPIKE: {total_ms:.1f}ms (avg {rolling_avg:.1f}ms) | "
                f"input={input_ms:.1f}ms streaming={streaming_ms:.1f}ms "
                f"scene_setup={scene.setup_ms:.1f}ms mesh_draw={scene.draw_ms:.1f}ms "
                f"mesh_cull={scene.cull_ms:.1f}ms "
                f"mesh_submit={scene.submit_ms:.1f}ms "
                f"gpu_query_wait={scene.gpu_query_wait_ms:.1f}ms "
                f"gpu_draw={gpu_draw_text} "
                f"recording_read={overlays.recording_read_ms:.1f}ms "
                f"recording_stage={overlays.recording_stage_ms:.1f}ms "
                f"recording_drain={overlays.recording_drain_ms:.1f}ms "
                f"overlay={overlays.overlay_ms:.1f}ms other={other_ms:.1f}ms | "
                f"drawn={scene.chunks_drawn}/{len(self._chunk_gpu_objects)} "
                f"loaded={stats['loaded']} pending={stats['pending']} "
                f"ready={stats.get('ready', 0)} "
                f"unload_pending={stats.get('unload_pending', 0)} "
                f"wanted={stats.get('wanted', 0)}"
            )
            _LOG.warning(
                "FRAME SPIKE STREAMING DETAIL: %s",
                self._format_streaming_frame_timing(streaming_timing),
            )

        self._frame_active_time_s += total_ms / 1000.0
        self._frame_count += 1
        now = time.time()
        if now - self._last_fps_print <= 2.0:
            return

        wall_interval_s = max(now - self._last_fps_print, 1e-6)
        active_interval_s = max(self._frame_active_time_s, 1e-6)
        rendered_fps = self._frame_count / active_interval_s
        wall_fps = self._frame_count / wall_interval_s
        if _LOG.isEnabledFor(logging.DEBUG):
            stats = self.world.stats()
            gpu_draw_text = self._format_optional_ms(self._last_gpu_draw_ms)
            speed_label = "manual_speed"
            displayed_speed = float(self.camera.move_speed)
            if benchmark_active:
                route_speed = getattr(
                    benchmark_controller.scenario,
                    "metadata",
                    {},
                ).get("actual_route_speed_m_per_second")
                if isinstance(route_speed, (int, float)):
                    speed_label = "route_speed"
                    displayed_speed = float(route_speed)
            _LOG.debug(
                f"rendered_fps={rendered_fps:.1f} wall_fps={wall_fps:.1f} "
                f"frame_cost={rolling_avg:.1f}ms "
                f"| chunks loaded={stats['loaded']} "
                f"pending={stats['pending']} "
                f"unload_pending={stats.get('unload_pending', 0)} "
                f"drawn={scene.chunks_drawn}/{len(self._chunk_gpu_objects)} "
                f"| {speed_label}={displayed_speed:.1f}m/s "
                f"| mesh_cull={scene.cull_ms:.1f}ms "
                f"mesh_submit={scene.submit_ms:.1f}ms "
                f"gpu_query_wait={scene.gpu_query_wait_ms:.1f}ms "
                f"recording_read={overlays.recording_read_ms:.1f}ms "
                f"recording_stage={overlays.recording_stage_ms:.1f}ms "
                f"recording_drain={overlays.recording_drain_ms:.1f}ms "
                f"gpu_draw={gpu_draw_text}"
            )
        self._frame_count = 0
        self._frame_active_time_s = 0.0
        self._last_fps_print = now

    def _render_interactive_frame(
        self,
        current_time: float,
        frame_time: float,
    ) -> None:
        """Render one interactive frame through explicit ordered stages."""
        frame_start = time.perf_counter()
        benchmark_controller = self._active_benchmark_controller()
        benchmark_active = (
            benchmark_controller is not None
            and not benchmark_controller.finished
        )
        self._update_texture_validation()
        input_ms = self._update_interactive_input(
            frame_time,
            benchmark_controller=benchmark_controller,
            benchmark_active=benchmark_active,
        )
        streaming_ms, streaming_timing, stats = (
            self._advance_interactive_streaming()
        )
        if self._render_initial_streaming_state(
            stats,
            benchmark_controller=benchmark_controller,
            benchmark_active=benchmark_active,
        ):
            return

        scene = self._render_interactive_scene(stats)
        overlays = self._render_interactive_overlays(scene)
        total_ms = (time.perf_counter() - frame_start) * 1000.0
        other_ms = max(
            0.0,
            total_ms
            - input_ms
            - streaming_ms
            - scene.setup_ms
            - scene.draw_ms
            - overlays.recording_read_ms
            - overlays.overlay_ms,
        )
        if benchmark_active and self._update_interactive_benchmark(
            benchmark_controller=benchmark_controller,
            scene=scene,
            overlays=overlays,
            total_ms=total_ms,
            streaming_ms=streaming_ms,
            other_ms=other_ms,
            stats=stats,
            streaming_timing=streaming_timing,
        ):
            return
        self._record_interactive_frame_telemetry(
            benchmark_controller=benchmark_controller,
            benchmark_active=benchmark_active,
            scene=scene,
            overlays=overlays,
            total_ms=total_ms,
            input_ms=input_ms,
            streaming_ms=streaming_ms,
            other_ms=other_ms,
            streaming_timing=streaming_timing,
        )
