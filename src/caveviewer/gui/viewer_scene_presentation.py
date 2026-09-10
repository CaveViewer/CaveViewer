"""Render-thread scene and HUD presentation for the native viewer.

The window supplies immutable geometry and layout facts. This component owns
the OpenGL state changes used to configure and draw the cave, alternate
recording framebuffer, right-column backplate, and fullscreen status scrim.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

import moderngl
import numpy as np


@dataclass(frozen=True)
class RightColumnLayoutInput:
    """Measured HUD facts needed to lay out the right-side control column."""

    window_size: tuple[int, int]
    stepper_widths: tuple[float, float, float]
    stepper_heights: tuple[float, float, float]
    label_widths: tuple[float, float, float]
    label_height: float
    button_width: float
    button_height: float
    panel_scale: float
    ui_scale: float
    bottom_margin: float
    bottom_padding: float
    right_margin: float
    side_padding: float
    top_padding: float
    label_gap: float
    column_gap: float
    button_group_gap: float


@dataclass(frozen=True)
class RightColumnLayout:
    """Stable draw and hit-test geometry for the right-side HUD."""

    brightness_anchor: tuple[float, float]
    ambient_anchor: tuple[float, float]
    render_distance_anchor: tuple[float, float]
    buttons_top_y: float
    button_right_inset: float
    content_bottom_inset: float
    content_center_x: float
    panel_rect: tuple[float, float, float, float]

    def as_dict(self) -> dict[str, object]:
        return dict(self.__dict__)


def right_column_layout(facts: RightColumnLayoutInput) -> RightColumnLayout:
    """Lay out the HUD from measured text/control facts without window state."""
    width, height = facts.window_size
    label_reserve = facts.label_height + 8 * facts.panel_scale
    content_bottom_inset = (
        facts.bottom_margin + facts.bottom_padding
    ) * facts.ui_scale
    buttons_bottom_y = height - content_bottom_inset
    buttons_top_y = buttons_bottom_y - facts.button_height
    content_width = max(
        *facts.stepper_widths,
        *facts.label_widths,
        facts.button_width,
    )
    side_padding = facts.side_padding * facts.ui_scale
    panel_right = width - facts.right_margin * facts.ui_scale
    panel_left = panel_right - content_width - 2 * side_padding
    content_center_x = panel_left + side_padding + content_width / 2.0

    render_bottom = buttons_top_y - facts.button_group_gap * facts.panel_scale
    render_anchor_y = render_bottom - facts.stepper_heights[2]
    ambient_bottom = (
        render_anchor_y - label_reserve - facts.column_gap * facts.panel_scale
    )
    ambient_anchor_y = ambient_bottom - facts.stepper_heights[1]
    brightness_bottom = (
        ambient_anchor_y - label_reserve - facts.column_gap * facts.panel_scale
    )
    brightness_anchor_y = brightness_bottom - facts.stepper_heights[0]
    anchors_x = tuple(
        content_center_x - stepper_width / 2.0
        for stepper_width in facts.stepper_widths
    )
    label_gap = facts.label_gap * facts.panel_scale
    panel_top = min(
        brightness_anchor_y - facts.label_height - label_gap,
        ambient_anchor_y - facts.label_height - label_gap,
        render_anchor_y - facts.label_height - label_gap,
    ) - facts.top_padding * facts.ui_scale
    panel_bottom = height - facts.bottom_margin * facts.ui_scale
    return RightColumnLayout(
        brightness_anchor=(anchors_x[0], brightness_anchor_y),
        ambient_anchor=(anchors_x[1], ambient_anchor_y),
        render_distance_anchor=(anchors_x[2], render_anchor_y),
        buttons_top_y=buttons_top_y,
        button_right_inset=width - (
            content_center_x + facts.button_width / 2.0
        ),
        content_bottom_inset=content_bottom_inset,
        content_center_x=content_center_x,
        panel_rect=(panel_left, panel_top, panel_right, panel_bottom),
    )


@dataclass(frozen=True)
class SceneDrawRequest:
    """Context-free facts for one cave mesh submission."""

    visible_cells: tuple[tuple[Any, list], ...]
    solid_enabled: bool
    wireframe_enabled: bool


@dataclass(frozen=True)
class SceneDrawTimings:
    """Measured CPU submission and optional GPU query timings."""

    draw_ms: float
    submit_ms: float
    gpu_query_wait_ms: float
    gpu_draw_ms: float | None


@dataclass(frozen=True)
class PresentedScene:
    """Scene facts and timings consumed by later interactive-frame stages."""

    view: np.ndarray
    projection: np.ndarray
    draw_request: SceneDrawRequest
    visual_stats: dict[str, Any]
    chunks_drawn: int
    setup_ms: float
    cull_ms: float
    draw_ms: float
    submit_ms: float
    gpu_query_wait_ms: float


@dataclass(frozen=True)
class OverlayTimings:
    """HUD and recording work measured after the main scene pass."""

    overlay_ms: float
    recording_read_ms: float
    recording_stage_ms: float
    recording_drain_ms: float


class ViewerScenePresentation:
    """Own scene and shared HUD OpenGL operations on the render thread."""

    def __init__(
        self,
        *,
        ctx: Any,
        program: Any,
        hud_panel_vbo: Any,
        hud_panel_vao: Any,
        status_panel_vbo: Any,
        status_panel_vao: Any,
        perf_counter: Callable[[], float],
    ) -> None:
        self.ctx = ctx
        self.program = program
        self.hud_panel_vbo = hud_panel_vbo
        self.hud_panel_vao = hud_panel_vao
        self.status_panel_vbo = status_panel_vbo
        self.status_panel_vao = status_panel_vao
        self.perf_counter = perf_counter

    def configure_scene(
        self,
        *,
        background: tuple[float, ...],
        view: np.ndarray,
        projection: np.ndarray,
        camera_position: np.ndarray,
        light_intensity: float,
        ambient: float,
        texture_enabled: bool,
    ) -> float:
        """Clear the screen and publish all per-frame scene uniforms."""
        started = self.perf_counter()
        self.ctx.clear(*background)
        self.program["u_view"].write(view.T.tobytes())
        self.program["u_projection"].write(projection.T.tobytes())
        position = tuple(float(value) for value in camera_position)
        self.program["u_camera_pos"].value = position
        self.program["u_light_color"].value = (1.0, 0.95, 0.85)
        self.program["u_light_intensity"].value = float(light_intensity)
        self.program["u_ambient"].value = float(ambient)
        self.program["u_texture_enabled"].value = bool(texture_enabled)
        return (self.perf_counter() - started) * 1000.0

    def draw_mesh(
        self,
        request: SceneDrawRequest,
        *,
        gpu_timer_enabled: bool,
    ) -> SceneDrawTimings:
        """Submit solid and wireframe passes and restore wireframe state."""
        draw_started = self.perf_counter()
        submit_started = self.perf_counter()
        gpu_query_wait_ms = 0.0
        gpu_draw_ms = None
        if gpu_timer_enabled:
            with self.ctx.query(time=True) as query:
                self._draw_visible_mesh(request)
            submit_ms = (self.perf_counter() - submit_started) * 1000.0
            wait_started = self.perf_counter()
            gpu_draw_ms = query.elapsed / 1_000_000
            gpu_query_wait_ms = (self.perf_counter() - wait_started) * 1000.0
        else:
            self._draw_visible_mesh(request)
            submit_ms = (self.perf_counter() - submit_started) * 1000.0
        return SceneDrawTimings(
            draw_ms=(self.perf_counter() - draw_started) * 1000.0,
            submit_ms=submit_ms,
            gpu_query_wait_ms=gpu_query_wait_ms,
            gpu_draw_ms=gpu_draw_ms,
        )

    def _draw_visible_mesh(self, request: SceneDrawRequest) -> None:
        self.program["u_texture"].value = 0
        if request.solid_enabled:
            for _cell, vao_list in request.visible_cells:
                for vao, _vbo, _material_name, texture in vao_list:
                    texture.use(location=0)
                    vao.render(moderngl.TRIANGLES)
        if not request.wireframe_enabled:
            return
        self.ctx.wireframe = True
        try:
            for _cell, vao_list in request.visible_cells:
                for vao, _vbo, _material_name, _texture in vao_list:
                    vao.render(moderngl.TRIANGLES)
        finally:
            self.ctx.wireframe = False

    def render_recording_frame(
        self,
        *,
        framebuffer: Any,
        output_size: tuple[int, int],
        background: tuple[float, ...],
        view: np.ndarray,
        screen_projection: np.ndarray,
        recording_projection: np.ndarray,
        request: SceneDrawRequest,
    ) -> None:
        """Draw the cave into an alternate framebuffer and restore the screen."""
        width, height = output_size
        previous_fbo = getattr(self.ctx, "fbo", None)
        previous_screen_viewport = getattr(self.ctx.screen, "viewport", None)
        previous_framebuffer_viewport = getattr(framebuffer, "viewport", None)
        try:
            framebuffer.use()
            framebuffer.viewport = (0, 0, width, height)
            self.ctx.clear(*background)
            self.program["u_projection"].write(recording_projection.T.tobytes())
            self.program["u_view"].write(view.T.tobytes())
            self._draw_visible_mesh(request)
        finally:
            try:
                target = previous_fbo if previous_fbo is not None else self.ctx.screen
                target.use()
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
            self.program["u_projection"].write(screen_projection.T.tobytes())
            self.program["u_view"].write(view.T.tobytes())

    def render_right_column_panel(
        self,
        *,
        window_size: tuple[int, int],
        panel_rect: tuple[float, float, float, float],
        fill_rgba: tuple[float, float, float, float],
        border_px: float,
        border_rgba: tuple[float, float, float, float],
    ) -> None:
        """Draw the shared backplate behind the right-side controls."""
        x0, y0, x1, y1 = panel_rect
        quads = [
            (x0, y0, x1, y1, fill_rgba),
            (x0, y0, x1, y0 + border_px, border_rgba),
            (x0, y1 - border_px, x1, y1, border_rgba),
            (x0, y0, x0 + border_px, y1, border_rgba),
            (x1 - border_px, y0, x1, y1, border_rgba),
        ]
        self._render_colored_quads(
            window_size=window_size,
            quads=quads,
            vbo=self.hud_panel_vbo,
            vao=self.hud_panel_vao,
        )

    def render_scrim(
        self,
        window_size: tuple[int, int],
        *,
        alpha: float,
    ) -> None:
        """Draw the fullscreen capture-status scrim."""
        width, height = window_size
        self._render_colored_quads(
            window_size=window_size,
            quads=[(0, 0, width, height, (0.001, 0.002, 0.005, alpha))],
            vbo=self.status_panel_vbo,
            vao=self.status_panel_vao,
        )

    def _render_colored_quads(
        self,
        *,
        window_size: tuple[int, int],
        quads: list[tuple[float, float, float, float, tuple[float, ...]]],
        vbo: Any,
        vao: Any,
    ) -> None:
        width, height = window_size
        vertices = []
        for x0, y0, x1, y1, rgba in quads:
            left = min((x0 / width) * 2.0 - 1.0, (x1 / width) * 2.0 - 1.0)
            right = max((x0 / width) * 2.0 - 1.0, (x1 / width) * 2.0 - 1.0)
            top = max(1.0 - (y0 / height) * 2.0, 1.0 - (y1 / height) * 2.0)
            bottom = min(1.0 - (y0 / height) * 2.0, 1.0 - (y1 / height) * 2.0)
            for x, y in (
                (left, bottom),
                (right, bottom),
                (right, top),
                (left, bottom),
                (right, top),
                (left, top),
            ):
                vertices.append((x, y, *rgba))
        vbo.write(np.asarray(vertices, dtype=np.float32).tobytes())
        self.ctx.disable(moderngl.CULL_FACE)
        self.ctx.disable(moderngl.DEPTH_TEST)
        self.ctx.enable(moderngl.BLEND)
        try:
            vao.render(moderngl.TRIANGLES, vertices=len(vertices))
        finally:
            self.ctx.disable(moderngl.BLEND)
            self.ctx.enable(moderngl.DEPTH_TEST)
            self.ctx.enable(moderngl.CULL_FACE)
