"""Tests for the render-thread scene and shared HUD presentation owner."""

from types import SimpleNamespace

import moderngl
import numpy as np
import pytest

from caveviewer.gui.viewer_scene_presentation import (
    RightColumnLayoutInput,
    SceneDrawRequest,
    ViewerScenePresentation,
    right_column_layout,
)


class _Uniform:
    def __init__(self, name, calls):
        self.name = name
        self.calls = calls
        self._value = None

    def write(self, value):
        self.calls.append(("write", self.name, value))

    @property
    def value(self):
        return self._value

    @value.setter
    def value(self, value):
        self._value = value
        self.calls.append(("value", self.name, value))


class _Program:
    def __init__(self, calls):
        self.uniforms = {
            name: _Uniform(name, calls)
            for name in (
                "u_view",
                "u_projection",
                "u_camera_pos",
                "u_light_color",
                "u_light_intensity",
                "u_ambient",
                "u_texture_enabled",
                "u_texture",
            )
        }

    def __getitem__(self, name):
        return self.uniforms[name]


class _Drawable:
    def __init__(self, name, calls, *, raises=False):
        self.name = name
        self.calls = calls
        self.raises = raises

    def use(self, *, location=None):
        self.calls.append(("use", self.name, location))

    def write(self, value):
        self.calls.append(("write-buffer", self.name, value))

    def render(self, mode, *, vertices=None):
        self.calls.append(("render", self.name, mode, vertices))
        if self.raises:
            raise RuntimeError(self.name)


class _Framebuffer:
    def __init__(self, name, calls, viewport):
        self.name = name
        self.calls = calls
        self.viewport = viewport

    def use(self):
        self.calls.append(("framebuffer", self.name))


class _Context:
    def __init__(self, calls):
        self.calls = calls
        self.wireframe = False
        self.screen = _Framebuffer("screen", calls, (0, 0, 800, 600))
        self.fbo = self.screen

    def clear(self, *color):
        self.calls.append(("clear", color))

    def disable(self, flag):
        self.calls.append(("disable", flag))

    def enable(self, flag):
        self.calls.append(("enable", flag))


def _presentation(calls, *, ctx=None, hud_vao=None, status_vao=None):
    ctx = ctx or _Context(calls)
    return ViewerScenePresentation(
        ctx=ctx,
        program=_Program(calls),
        hud_panel_vbo=_Drawable("hud-vbo", calls),
        hud_panel_vao=hud_vao or _Drawable("hud-vao", calls),
        status_panel_vbo=_Drawable("status-vbo", calls),
        status_panel_vao=status_vao or _Drawable("status-vao", calls),
        perf_counter=iter((1.0, 1.0, 1.003, 1.006)).__next__,
    )


def test_right_column_layout_centers_every_control_in_widest_content():
    layout = right_column_layout(
        RightColumnLayoutInput(
            window_size=(1000, 700),
            stepper_widths=(80.0, 100.0, 90.0),
            stepper_heights=(30.0, 30.0, 30.0),
            label_widths=(70.0, 180.0, 90.0),
            label_height=18.0,
            button_width=120.0,
            button_height=160.0,
            panel_scale=1.0,
            ui_scale=1.0,
            bottom_margin=15.0,
            bottom_padding=12.0,
            right_margin=16.0,
            side_padding=10.0,
            top_padding=12.0,
            label_gap=6.0,
            column_gap=8.0,
            button_group_gap=10.0,
        )
    )

    assert layout.panel_rect == pytest.approx((784.0, 309.0, 984.0, 685.0))
    assert layout.content_center_x == pytest.approx(884.0)
    assert layout.brightness_anchor == pytest.approx((844.0, 345.0))
    assert layout.ambient_anchor == pytest.approx((834.0, 409.0))
    assert layout.render_distance_anchor == pytest.approx((839.0, 473.0))
    assert layout.buttons_top_y == pytest.approx(513.0)
    assert layout.button_right_inset == pytest.approx(56.0)


def test_mesh_draw_submits_solid_before_wireframe_and_restores_state():
    calls = []
    presentation = _presentation(calls)
    texture = _Drawable("texture", calls)
    vao = _Drawable("mesh", calls)

    timing = presentation.draw_mesh(
        SceneDrawRequest(
            visible_cells=((SimpleNamespace(), [(vao, object(), "rock", texture)]),),
            solid_enabled=True,
            wireframe_enabled=True,
        ),
        gpu_timer_enabled=False,
    )

    mesh_renders = [call for call in calls if call[:2] == ("render", "mesh")]
    assert calls.index(("use", "texture", 0)) < calls.index(mesh_renders[0])
    assert mesh_renders == [
        ("render", "mesh", moderngl.TRIANGLES, None),
        ("render", "mesh", moderngl.TRIANGLES, None),
    ]
    assert presentation.ctx.wireframe is False
    assert timing.draw_ms == pytest.approx(6.0)
    assert timing.submit_ms == pytest.approx(3.0)


def test_mesh_draw_restores_wireframe_when_submission_fails():
    calls = []
    presentation = _presentation(calls)
    vao = _Drawable("broken-mesh", calls, raises=True)

    with pytest.raises(RuntimeError, match="broken-mesh"):
        presentation.draw_mesh(
            SceneDrawRequest(
                visible_cells=((SimpleNamespace(), [(vao, object(), "rock", object())]),),
                solid_enabled=False,
                wireframe_enabled=True,
            ),
            gpu_timer_enabled=False,
        )

    assert presentation.ctx.wireframe is False


def test_recording_frame_restores_framebuffers_viewports_and_uniforms():
    calls = []
    ctx = _Context(calls)
    previous = _Framebuffer("previous", calls, (4, 5, 640, 480))
    ctx.fbo = previous
    target = _Framebuffer("recording", calls, (7, 8, 9, 10))
    presentation = _presentation(calls, ctx=ctx)
    screen_projection = np.eye(4, dtype=np.float32)
    recording_projection = np.full((4, 4), 2.0, dtype=np.float32)

    presentation.render_recording_frame(
        framebuffer=target,
        output_size=(320, 180),
        background=(0.1, 0.2, 0.3, 1.0),
        view=np.full((4, 4), 3.0, dtype=np.float32),
        screen_projection=screen_projection,
        recording_projection=recording_projection,
        request=SceneDrawRequest((), solid_enabled=False, wireframe_enabled=False),
    )

    assert ("framebuffer", "recording") in calls
    assert calls.index(("framebuffer", "recording")) < calls.index(
        ("framebuffer", "previous")
    )
    assert target.viewport == (7, 8, 9, 10)
    assert ctx.screen.viewport == (0, 0, 800, 600)
    assert ctx.wireframe is False
    projection_writes = [
        call for call in calls if call[:2] == ("write", "u_projection")
    ]
    assert projection_writes == [
        ("write", "u_projection", recording_projection.T.tobytes()),
        ("write", "u_projection", screen_projection.T.tobytes()),
    ]


def test_panel_draw_restores_gl_state_when_render_fails():
    calls = []
    presentation = _presentation(
        calls,
        hud_vao=_Drawable("broken-panel", calls, raises=True),
    )

    with pytest.raises(RuntimeError, match="broken-panel"):
        presentation.render_right_column_panel(
            window_size=(800, 600),
            panel_rect=(600.0, 100.0, 780.0, 580.0),
            fill_rgba=(0.0, 0.0, 0.0, 0.5),
            border_px=1.0,
            border_rgba=(1.0, 1.0, 1.0, 1.0),
        )

    assert calls[-3:] == [
        ("disable", moderngl.BLEND),
        ("enable", moderngl.DEPTH_TEST),
        ("enable", moderngl.CULL_FACE),
    ]
