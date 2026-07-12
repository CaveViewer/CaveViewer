"""Exercise Linux GLFW protocol selection, hints, and fallback boundaries."""

from __future__ import annotations

import os
import sys
from types import SimpleNamespace

import pytest

from caveviewer.gui.platform.app_identity import LINUX_WINDOW_INSTANCE_NAME
from caveviewer.gui.platform.windowing import (
    WINDOW_SYSTEM_ENV_VAR,
    WindowBackendError,
    WindowSystem,
    resolve_window_backend_plan,
    run_window_config,
)
from caveviewer.version import APPLICATION_ID


class FakeGlfw:
    PLATFORM = 1
    PLATFORM_WAYLAND = 2
    PLATFORM_X11 = 3
    WAYLAND_APP_ID = 4
    X11_CLASS_NAME = 5
    X11_INSTANCE_NAME = 6
    SCALE_TO_MONITOR = 7
    RESIZABLE = 8
    DECORATED = 9
    WAYLAND_LIBDECOR = 10
    WAYLAND_PREFER_LIBDECOR = 11
    TRUE = 1
    FALSE = 0

    def __init__(
        self,
        *,
        init_result=True,
        supported=True,
        content_scale=(1.0, 1.0),
        video_mode=(1920, 1080),
    ):
        self.init_result = init_result
        self.supported = supported
        self.selected_platform = None
        self.calls = []
        self.content_scale = content_scale
        self.video_mode = video_mode

    def platform_supported(self, platform):
        self.calls.append(("platform_supported", platform))
        return self.supported

    def init_hint(self, hint, value):
        self.calls.append(("init_hint", hint, value))
        if hint == self.PLATFORM:
            self.selected_platform = value

    def init(self):
        self.calls.append(("init",))
        return self.init_result

    def get_error(self):
        return 1, b"display unavailable"

    def window_hint_string(self, hint, value):
        self.calls.append(("window_hint_string", hint, value))

    def get_platform(self):
        return self.selected_platform

    def terminate(self):
        self.calls.append(("terminate",))

    def get_primary_monitor(self):
        self.calls.append(("get_primary_monitor",))
        return "primary"

    def get_monitor_workarea(self, monitor):
        self.calls.append(("get_monitor_workarea", monitor))
        return 0, 40, 1920, 1040

    def window_hint(self, hint, value):
        self.calls.append(("window_hint", hint, value))

    def get_monitor_content_scale(self, monitor):
        self.calls.append(("get_monitor_content_scale", monitor))
        return self.content_scale

    def get_video_mode(self, monitor):
        self.calls.append(("get_video_mode", monitor))
        return self.video_mode


def test_auto_plan_prefers_x11_then_wayland_when_both_are_available():
    plan = resolve_window_backend_plan(
        environ={
            "XDG_SESSION_TYPE": "wayland",
            "WAYLAND_DISPLAY": "wayland-0",
            "DISPLAY": ":0",
        },
        platform_name="linux",
    )

    assert plan.mode is WindowSystem.AUTO
    assert plan.attempts == (WindowSystem.X11, WindowSystem.WAYLAND)


@pytest.mark.parametrize("mode", ["wayland", "x11"])
def test_explicit_plan_never_adds_a_fallback(mode):
    plan = resolve_window_backend_plan(
        environ={WINDOW_SYSTEM_ENV_VAR: mode, "DISPLAY": ":0"},
        platform_name="linux",
    )

    assert plan.attempts == (WindowSystem(mode),)


def test_invalid_window_system_is_actionable():
    with pytest.raises(WindowBackendError, match="expected one of"):
        resolve_window_backend_plan(
            environ={WINDOW_SYSTEM_ENV_VAR: "mir"}, platform_name="linux"
        )


def test_non_linux_platform_keeps_existing_moderngl_backend():
    calls = []

    run_window_config(
        object,
        runner=lambda config, args: calls.append((config, args)),
        environ={},
        platform_name="darwin",
        glfw_loader=lambda _system: pytest.fail("GLFW must remain Linux-only"),
    )

    assert calls == [(object, [])]


def test_glfw_hints_identity_before_window_creation(monkeypatch):
    glfw = FakeGlfw()
    calls = []
    monkeypatch.setenv("MODERNGL_WINDOW", "pyglet")

    run_window_config(
        object,
        runner=lambda config, args: calls.append(
            (config, args, list(glfw.calls), os.environ["MODERNGL_WINDOW"])
        ),
        environ={WINDOW_SYSTEM_ENV_VAR: "wayland"},
        platform_name="linux",
        glfw_loader=lambda _system: glfw,
    )

    assert calls[0][0:2] == (object, ["--window", "glfw"])
    assert calls[0][3] == "glfw"
    assert os.environ["MODERNGL_WINDOW"] == "pyglet"
    calls_before_runner = calls[0][2]
    assert ("init_hint", glfw.PLATFORM, glfw.PLATFORM_WAYLAND) in calls_before_runner
    assert (
        "window_hint_string",
        glfw.WAYLAND_APP_ID,
        APPLICATION_ID,
    ) in calls_before_runner
    assert (
        "window_hint_string",
        glfw.X11_CLASS_NAME,
        APPLICATION_ID,
    ) in calls_before_runner
    assert (
        "window_hint_string",
        glfw.X11_INSTANCE_NAME,
        LINUX_WINDOW_INSTANCE_NAME,
    ) in calls_before_runner
    assert (
        "init_hint",
        glfw.WAYLAND_LIBDECOR,
        glfw.WAYLAND_PREFER_LIBDECOR,
    ) in calls_before_runner


def test_relative_size_uses_glfw_workarea_without_duplicate_dpi_scaling():
    glfw = FakeGlfw()

    class Config:
        window_size = (1600, 1000)

    observed = []

    def runner(config, args):
        # Reproduce the pinned ModernGL-window GLFW backend's unconditional
        # hint. The adapter must turn it off for an already-relative size.
        glfw.window_hint(glfw.SCALE_TO_MONITOR, glfw.TRUE)
        observed.append((config.window_size, args))

    run_window_config(
        Config,
        runner=runner,
        environ={WINDOW_SYSTEM_ENV_VAR: "x11"},
        platform_name="linux",
        glfw_loader=lambda _system: glfw,
        window_size_fraction=0.8,
        fallback_window_size=(1600, 1000),
    )

    assert observed == [((1536, 832), ["--window", "glfw"])]
    assert ("window_hint", glfw.SCALE_TO_MONITOR, glfw.FALSE) in glfw.calls
    assert ("get_monitor_content_scale", "primary") not in glfw.calls

    # The override is scoped to window creation and does not mutate pyGLFW.
    glfw.window_hint(glfw.SCALE_TO_MONITOR, glfw.TRUE)
    assert glfw.calls[-1] == ("window_hint", glfw.SCALE_TO_MONITOR, glfw.TRUE)


def test_viewer_launch_can_force_glfw_window_resizable_and_decorated():
    glfw = FakeGlfw()

    class Config:
        window_size = (1600, 1000)

    observed = []

    def runner(config, args):
        # Reproduce a backend/runtime path that would otherwise leave the
        # viewer fixed-size or without compositor resize handles.
        glfw.window_hint(glfw.RESIZABLE, glfw.FALSE)
        glfw.window_hint(glfw.DECORATED, glfw.FALSE)
        glfw.window_hint(glfw.SCALE_TO_MONITOR, glfw.TRUE)
        observed.append((config.window_size, args))

    run_window_config(
        Config,
        runner=runner,
        environ={WINDOW_SYSTEM_ENV_VAR: "x11"},
        platform_name="linux",
        glfw_loader=lambda _system: glfw,
        window_size_fraction=0.8,
        fallback_window_size=(1600, 1000),
        force_resizable_window=True,
    )

    assert observed == [((1536, 832), ["--window", "glfw"])]
    assert ("window_hint", glfw.RESIZABLE, glfw.TRUE) in glfw.calls
    assert ("window_hint", glfw.DECORATED, glfw.TRUE) in glfw.calls
    assert ("window_hint", glfw.SCALE_TO_MONITOR, glfw.FALSE) in glfw.calls


def test_wayland_relative_size_converts_physical_workarea_to_logical_coordinates():
    glfw = FakeGlfw(content_scale=(2.0, 2.0), video_mode=(3840, 2160))
    glfw.get_monitor_workarea = lambda monitor: (0, 40, 3840, 2160)

    class Config:
        window_size = (1600, 1000)

    observed = []
    run_window_config(
        Config,
        runner=lambda config, args: observed.append((config.window_size, args)),
        environ={WINDOW_SYSTEM_ENV_VAR: "wayland"},
        platform_name="linux",
        glfw_loader=lambda _system: glfw,
        window_size_fraction=0.8,
        fallback_window_size=(1600, 1000),
    )

    assert observed == [((1536, 864), ["--window", "glfw"])]


def test_wayland_relative_size_keeps_already_logical_workarea_coordinates():
    glfw = FakeGlfw(content_scale=(2.0, 2.0), video_mode=(3840, 2160))
    glfw.get_monitor_workarea = lambda monitor: (0, 40, 1920, 1080)

    class Config:
        window_size = (1600, 1000)

    observed = []
    run_window_config(
        Config,
        runner=lambda config, args: observed.append((config.window_size, args)),
        environ={WINDOW_SYSTEM_ENV_VAR: "wayland"},
        platform_name="linux",
        glfw_loader=lambda _system: glfw,
        window_size_fraction=0.8,
        fallback_window_size=(1600, 1000),
    )

    assert observed == [((1536, 864), ["--window", "glfw"])]


def test_relative_size_uses_safe_fallback_when_workarea_is_unavailable():
    glfw = FakeGlfw()
    glfw.get_primary_monitor = lambda: None

    class Config:
        window_size = (10, 10)

    observed = []
    run_window_config(
        Config,
        runner=lambda config, **_kwargs: observed.append(config.window_size),
        environ={WINDOW_SYSTEM_ENV_VAR: "wayland"},
        platform_name="linux",
        glfw_loader=lambda _system: glfw,
        window_size_fraction=0.8,
        fallback_window_size=(1280, 720),
    )

    assert observed == [(1280, 720)]


def test_auto_mode_retries_wayland_after_x11_initialization_failure():
    x11 = FakeGlfw(init_result=False)
    wayland = FakeGlfw()
    loaded = []
    runs = []

    def load(system):
        loaded.append(system)
        return wayland if system is WindowSystem.WAYLAND else x11

    run_window_config(
        object,
        runner=lambda _config, args: runs.append(args),
        environ={"WAYLAND_DISPLAY": "wayland-0", "DISPLAY": ":0"},
        platform_name="linux",
        glfw_loader=load,
    )

    assert loaded == [WindowSystem.X11, WindowSystem.WAYLAND]
    assert ("terminate",) in x11.calls
    assert runs == [["--window", "glfw"]]


def test_auto_mode_retries_only_known_window_creation_failure():
    wayland = FakeGlfw()
    x11 = FakeGlfw()
    attempts = []

    def runner(_config, args):
        assert args == ["--window", "glfw"]
        attempts.append(True)
        if len(attempts) == 1:
            raise ValueError("Failed to create window")

    run_window_config(
        object,
        runner=runner,
        environ={"WAYLAND_DISPLAY": "wayland-0", "DISPLAY": ":0"},
        platform_name="linux",
        glfw_loader=lambda system: (
            wayland if system is WindowSystem.WAYLAND else x11
        ),
    )

    assert len(attempts) == 2


def test_wayland_uses_egl_to_detect_current_glfw_context(monkeypatch):
    glfw = FakeGlfw()
    created_context = object()
    context_calls = []

    class Config:
        gl_version = (3, 3)

        @classmethod
        def init_mgl_context(cls):
            return None

    def create_context(**options):
        context_calls.append(options)
        return created_context

    monkeypatch.setitem(
        sys.modules,
        "moderngl",
        SimpleNamespace(create_context=create_context),
    )

    observed = []

    def runner(config, args):
        observed.append((config.init_mgl_context(), args))

    run_window_config(
        Config,
        runner=runner,
        environ={WINDOW_SYSTEM_ENV_VAR: "wayland"},
        platform_name="linux",
        glfw_loader=lambda _system: glfw,
    )

    assert observed == [(created_context, ["--window", "glfw"])]
    assert context_calls == [
        {"require": 330, "share": True, "backend": "egl"}
    ]
    assert Config.init_mgl_context() is None


def test_auto_mode_retries_wayland_after_x11_context_detection_failure():
    wayland = FakeGlfw()
    x11 = FakeGlfw()
    loaded = []
    runs = []

    def runner(_config, args):
        runs.append(args)
        if len(runs) == 1:
            raise Exception(
                "(share) eglGetCurrentContext: cannot detect OpenGL context"
            )

    run_window_config(
        object,
        runner=runner,
        environ={"WAYLAND_DISPLAY": "wayland-0", "DISPLAY": ":0"},
        platform_name="linux",
        glfw_loader=lambda system: loaded.append(system)
        or (wayland if system is WindowSystem.WAYLAND else x11),
    )

    assert loaded == [WindowSystem.X11, WindowSystem.WAYLAND]
    assert runs == [["--window", "glfw"], ["--window", "glfw"]]


def test_render_configuration_failure_does_not_trigger_backend_fallback():
    loaded = []

    with pytest.raises(RuntimeError, match="shader failed"):
        run_window_config(
            object,
            runner=lambda *_args, **_kwargs: (_ for _ in ()).throw(
                RuntimeError("shader failed")
            ),
            environ={"WAYLAND_DISPLAY": "wayland-0", "DISPLAY": ":0"},
            platform_name="linux",
            glfw_loader=lambda system: loaded.append(system) or FakeGlfw(),
        )

    assert loaded == [WindowSystem.X11]


def test_explicit_wayland_failure_does_not_fall_back():
    loaded = []

    with pytest.raises(WindowBackendError, match="wayland"):
        run_window_config(
            object,
            runner=lambda *_args, **_kwargs: None,
            environ={WINDOW_SYSTEM_ENV_VAR: "wayland", "DISPLAY": ":0"},
            platform_name="linux",
            glfw_loader=lambda system: loaded.append(system)
            or FakeGlfw(init_result=False),
        )

    assert loaded == [WindowSystem.WAYLAND]
