"""Tests for CaveViewer map-session and top-level application control flow."""

from __future__ import annotations

import json
import os
import sys
import traceback
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from caveviewer import app
from caveviewer.core.chunking import builder as chunker
from caveviewer.core.preferences.runtime_settings import RuntimePlatformFacts
from caveviewer.core.release_metadata import (
    ReleaseMetadata,
    ReleaseMetadataSource,
)
from caveviewer.gui.manual_dive_trace import MANUAL_DIVE_TRACE_SCHEMA_VERSION
from caveviewer.gui.platform import runtime as platform_runtime_module
from caveviewer.gui.platform.app_identity import tk_root_options


class _LogRecorder:
    def __init__(self):
        self.info_messages = []
        self.warning_messages = []
        self.error_messages = []

    @staticmethod
    def _format(message, args):
        return message % args if args else str(message)

    def info(self, message, *args):
        self.info_messages.append(self._format(message, args))

    def warning(self, message, *args):
        self.warning_messages.append(self._format(message, args))

    def error(self, message, *args):
        self.error_messages.append(self._format(message, args))


def _guided_dive_cache_identity() -> dict[str, int | str]:
    return {
        "version": 1,
        "source_sha256": "a" * 64,
        "cache_manifest_sha256": "b" * 64,
    }


def _install_viewer_module(monkeypatch, *, run_viewer=None, run_pending=None):
    viewer = ModuleType("caveviewer.gui.viewer_window")
    viewer.run_viewer = run_viewer or (lambda *_args, **_kwargs: None)
    viewer.run_viewer_with_pending_import = run_pending or (
        lambda *_args, **_kwargs: None
    )
    monkeypatch.setitem(sys.modules, "caveviewer.gui.viewer_window", viewer)
    return viewer


def _install_splash_module(monkeypatch, callback):
    splash = ModuleType("caveviewer.gui.splash_screen")
    splash.show_splash_screen = callback
    monkeypatch.setitem(sys.modules, "caveviewer.gui.splash_screen", splash)


def _install_update_manager_module(monkeypatch):
    update_manager_module = ModuleType("caveviewer.gui.update_manager")
    instances = []

    class FakeUpdateManager:
        def __init__(self, current_version, *, platform_runtime=None):
            self.current_version = current_version
            self.platform_runtime = platform_runtime
            self.shutdown_calls = 0
            instances.append(self)

        def shutdown(self):
            self.shutdown_calls += 1

    update_manager_module.UpdateManager = FakeUpdateManager
    monkeypatch.setitem(
        sys.modules,
        "caveviewer.gui.update_manager",
        update_manager_module,
    )
    return instances


def test_map_session_opens_selected_prebuilt_cache_folder(tmp_path, monkeypatch):
    (tmp_path / chunker.MANIFEST_NAME).write_text("{}", encoding="utf-8")
    opened = []
    _install_viewer_module(
        monkeypatch,
        run_viewer=lambda *args, **kwargs: opened.append((args, kwargs)),
    )
    monkeypatch.setattr(chunker, "cache_chunk_size", lambda _path: 8.0)
    monkeypatch.setattr(chunker, "configured_chunk_size", lambda: 8.0)

    app._run_map_session(str(tmp_path))

    assert opened == [
        (
            (str(tmp_path),),
            {"textures_dir": str(tmp_path), "map_root": str(tmp_path)},
        )
    ]


def test_map_session_forwards_an_injected_runtime_to_the_viewer(tmp_path, monkeypatch):
    (tmp_path / chunker.MANIFEST_NAME).write_text("{}", encoding="utf-8")
    opened = []
    runtime = object()
    _install_viewer_module(
        monkeypatch,
        run_viewer=lambda *args, **kwargs: opened.append((args, kwargs)),
    )
    monkeypatch.setattr(chunker, "cache_chunk_size", lambda _path: 8.0)
    monkeypatch.setattr(chunker, "configured_chunk_size", lambda: 8.0)

    app._run_map_session(str(tmp_path), platform_runtime=runtime)

    assert opened == [
        (
            (str(tmp_path),),
            {
                "textures_dir": str(tmp_path),
                "map_root": str(tmp_path),
                "platform_runtime": runtime,
            },
        )
    ]


def test_map_session_ignores_old_adjacent_prebuilt_cache_layouts(
    tmp_path, monkeypatch
):
    old_cache = tmp_path / "_cache"
    old_cache.mkdir()
    (old_cache / chunker.MANIFEST_NAME).write_text("{}", encoding="utf-8")
    old_legacy = tmp_path / ".caveviewer_cache"
    old_legacy.mkdir()
    (old_legacy / chunker.MANIFEST_NAME).write_text("{}", encoding="utf-8")
    opened = []
    _install_viewer_module(
        monkeypatch,
        run_viewer=lambda *args, **kwargs: opened.append((args, kwargs)),
    )

    with pytest.raises(SystemExit) as raised:
        app._run_map_session(str(tmp_path))

    assert raised.value.code == 1
    assert opened == []


def test_map_session_reports_missing_model_and_cache(tmp_path, monkeypatch):
    recorder = _LogRecorder()
    monkeypatch.setattr(app, "_LOG", recorder)

    with pytest.raises(SystemExit) as raised:
        app._run_map_session(str(tmp_path))

    assert raised.value.code == 1
    assert "No supported model file" in recorder.error_messages[-1]


def test_map_session_reports_prebuilt_viewer_failure(tmp_path, monkeypatch):
    (tmp_path / chunker.MANIFEST_NAME).write_text("{}", encoding="utf-8")
    recorder = _LogRecorder()
    printed = []
    shown = []
    runtime_stages = []
    runtime_errors = []
    monkeypatch.setattr(app, "_LOG", recorder)
    monkeypatch.setattr(traceback, "print_exc", lambda: printed.append(True))
    monkeypatch.setattr(
        app,
        "_show_viewer_launch_error",
        lambda error: shown.append(str(error)),
    )
    monkeypatch.setattr(
        app,
        "record_runtime_stage",
        lambda stage, **context: runtime_stages.append((stage, context)),
    )
    monkeypatch.setattr(
        app,
        "record_runtime_exception",
        lambda stage, error, **context: runtime_errors.append(
            (stage, str(error), context)
        ),
    )
    monkeypatch.setattr(chunker, "cache_chunk_size", lambda _path: 8.0)
    monkeypatch.setattr(chunker, "configured_chunk_size", lambda: 8.0)

    def fail_viewer(*_args, **_kwargs):
        raise RuntimeError("OpenGL unavailable")

    _install_viewer_module(monkeypatch, run_viewer=fail_viewer)

    with pytest.raises(SystemExit) as raised:
        app._run_map_session(str(tmp_path))

    assert raised.value.code == 1
    assert "OpenGL unavailable" in recorder.error_messages[-1]
    assert printed == [True]
    assert shown == ["OpenGL unavailable"]
    assert [stage for stage, _context in runtime_stages] == [
        "map_session_selected",
        "viewer_session_launch_requested",
    ]
    assert runtime_errors == [
        (
            "viewer_session_exception",
            "OpenGL unavailable",
            {"cache_dir": str(tmp_path)},
        )
    ]


def test_map_session_opens_obj_with_existing_cache(tmp_path, monkeypatch):
    source = tmp_path / "map.obj"
    material = tmp_path / "map.mtl"
    cache_dir = tmp_path / "built-cache"
    descriptor = {
        "format": "obj",
        "obj_path": str(source),
        "mtl_path": str(material),
    }
    opened = []
    monkeypatch.setattr(app, "find_model_file", lambda _folder: descriptor)
    monkeypatch.setattr(chunker, "cache_is_valid", lambda _path: True)
    monkeypatch.setattr(chunker, "get_cache_dir", lambda _path: str(cache_dir))
    monkeypatch.setattr(chunker, "cache_chunk_size", lambda _path: 8.0)
    monkeypatch.setattr(chunker, "configured_chunk_size", lambda: 8.0)
    _install_viewer_module(
        monkeypatch,
        run_viewer=lambda *args, **kwargs: opened.append((args, kwargs)),
    )

    app._run_map_session(str(tmp_path))

    assert opened == [
        (
            (str(cache_dir),),
            {"textures_dir": str(cache_dir), "map_root": str(tmp_path)},
        )
    ]


def test_map_session_opens_uncached_glb_with_pending_import(tmp_path, monkeypatch):
    descriptor = {"format": "glb", "glb_path": str(tmp_path / "map.glb")}
    opened = []
    monkeypatch.setattr(app, "find_model_file", lambda _folder: descriptor)
    monkeypatch.setattr(chunker, "cache_is_valid", lambda _path: False)
    _install_viewer_module(
        monkeypatch,
        run_pending=lambda *args, **kwargs: opened.append((args, kwargs)),
    )

    app._run_map_session(str(tmp_path))

    assert opened == [((descriptor,), {"textures_dir": str(tmp_path)})]


def test_map_session_opens_uncached_direct_glb_file_with_parent_texture_dir(
    tmp_path, monkeypatch
):
    source = tmp_path / "map.glb"
    source.write_bytes(b"glTF")
    descriptor = {"format": "glb", "glb_path": str(source)}
    opened = []
    monkeypatch.setattr(chunker, "cache_is_valid", lambda _path: False)
    _install_viewer_module(
        monkeypatch,
        run_pending=lambda *args, **kwargs: opened.append((args, kwargs)),
    )

    app._run_map_session(str(source))

    assert opened == [((descriptor,), {"textures_dir": str(tmp_path)})]


@pytest.mark.parametrize("cache_valid", [True, False])
def test_map_session_opens_recorded_dive_against_its_map_local_cache(
    tmp_path,
    monkeypatch,
    cache_valid,
):
    map_dir = tmp_path / "Devils Eye"
    trace_dir = map_dir / "_guided_dives"
    cache_dir = map_dir / "_cache"
    trace_dir.mkdir(parents=True)
    cache_dir.mkdir()
    source = map_dir / "cave.obj"
    source.write_text("v 0 0 0\n", encoding="utf-8")
    trace_path = trace_dir / "favorite.jsonl"
    records = [
        {
            "record": "trace_started",
            "schema_version": MANUAL_DIVE_TRACE_SCHEMA_VERSION,
            "session_id": "shared-dive",
            "map": {
                "source_obj": "cave.obj",
                "manifest_version": 1,
                "chunk_size_m": 50.0,
                "triangle_count": 12,
                "cache_identity": _guided_dive_cache_identity(),
                "coordinate_space": "manifest_xyz",
                "distance_unit": "meter",
                "orientation_unit": "radian",
            },
        },
        {
            "record": "sample",
            "schema_version": MANUAL_DIVE_TRACE_SCHEMA_VERSION,
            "session_id": "shared-dive",
            "sample_index": 0,
            "elapsed_s": 0.0,
            "position": [1.0, 2.0, 3.0],
            "forward": [1.0, 0.0, 0.0],
            "up": [0.0, 1.0, 0.0],
            "right": [0.0, 0.0, 1.0],
            "yaw": 0.0,
            "pitch": 0.0,
            "roll": 0.0,
            "move_speed_m_per_second": 4.0,
        },
        {
            "record": "trace_completed",
            "schema_version": MANUAL_DIVE_TRACE_SCHEMA_VERSION,
            "session_id": "shared-dive",
            "duration_s": 0.0,
        },
    ]
    trace_path.write_text(
        "".join(json.dumps(record) + "\n" for record in records),
        encoding="utf-8",
    )
    descriptor = {
        "format": "obj",
        "obj_path": str(source),
        "mtl_path": str(map_dir / "cave.mtl"),
    }
    manifest = {
        "version": 1,
        "source_obj": "cave.obj",
        "chunk_size": 50.0,
        "triangle_count": 12,
        "guided_dive_identity": _guided_dive_cache_identity(),
    }
    opened = []
    monkeypatch.setattr(app, "find_model_file", lambda path: descriptor)
    monkeypatch.setattr(chunker, "cache_is_valid", lambda _path: cache_valid)
    monkeypatch.setattr(chunker, "get_cache_dir", lambda _path: str(cache_dir))
    monkeypatch.setattr(chunker, "load_manifest", lambda _path: manifest)
    monkeypatch.setattr(chunker, "cache_chunk_size", lambda _path: 50.0)
    monkeypatch.setattr(chunker, "configured_chunk_size", lambda: 50.0)
    _install_viewer_module(
        monkeypatch,
        run_viewer=lambda *args, **kwargs: opened.append((args, kwargs)),
        run_pending=lambda *args, **kwargs: opened.append((args, kwargs)),
    )

    app._run_map_session(str(trace_path))

    assert len(opened) == 1
    args, kwargs = opened[0]
    if cache_valid:
        assert args == (str(cache_dir),)
        assert kwargs["textures_dir"] == str(cache_dir)
        assert kwargs["map_root"] == str(map_dir)
    else:
        assert args == (descriptor,)
        assert kwargs["textures_dir"] == str(map_dir)
    assert kwargs["recorded_dive_trace"].path == trace_path.resolve()
    assert kwargs["recorded_dive_trace"].initial_pose.position == (1.0, 2.0, 3.0)


def test_map_session_rejects_direct_unsupported_file(tmp_path, monkeypatch):
    payload = tmp_path / "notes.txt"
    payload.write_text("not a cave map", encoding="utf-8")
    recorder = _LogRecorder()
    monkeypatch.setattr(app, "_LOG", recorder)

    with pytest.raises(SystemExit) as raised:
        app._run_map_session(str(payload))

    assert raised.value.code == 1
    assert "No supported model file" in recorder.error_messages[-1]


def test_map_session_reports_an_incomplete_recorded_dive(tmp_path, monkeypatch):
    trace_path = tmp_path / "broken.jsonl"
    trace_path.write_text(
        json.dumps(
            {
                "record": "trace_started",
                "schema_version": MANUAL_DIVE_TRACE_SCHEMA_VERSION,
                "session_id": "broken",
                "map": {},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    recorder = _LogRecorder()
    monkeypatch.setattr(app, "_LOG", recorder)

    with pytest.raises(SystemExit) as raised:
        app._run_map_session(str(trace_path))

    assert raised.value.code == 1
    assert "trace_completed" in recorder.error_messages[-1]


@pytest.mark.parametrize("cache_is_valid", [True, False])
def test_map_session_reports_source_viewer_failures(
    tmp_path, monkeypatch, cache_is_valid
):
    descriptor = {"format": "glb", "glb_path": str(tmp_path / "map.glb")}
    recorder = _LogRecorder()
    printed = []
    shown = []
    monkeypatch.setattr(app, "_LOG", recorder)
    monkeypatch.setattr(app, "find_model_file", lambda _folder: descriptor)
    monkeypatch.setattr(chunker, "cache_is_valid", lambda _path: cache_is_valid)
    monkeypatch.setattr(chunker, "get_cache_dir", lambda _path: "/cache/map")
    monkeypatch.setattr(chunker, "cache_chunk_size", lambda _path: 8.0)
    monkeypatch.setattr(chunker, "configured_chunk_size", lambda: 8.0)
    monkeypatch.setattr(traceback, "print_exc", lambda: printed.append(True))
    monkeypatch.setattr(
        app,
        "_show_viewer_launch_error",
        lambda error: shown.append(str(error)),
    )

    def fail_viewer(*_args, **_kwargs):
        raise RuntimeError("viewer failed")

    _install_viewer_module(
        monkeypatch, run_viewer=fail_viewer, run_pending=fail_viewer
    )

    with pytest.raises(SystemExit) as raised:
        app._run_map_session(str(tmp_path))

    assert raised.value.code == 1
    assert "viewer failed" in recorder.error_messages[-1]
    assert printed == [True]
    assert shown == ["viewer failed"]


def _prepare_main(monkeypatch):
    recorder = _LogRecorder()
    configured = []
    monkeypatch.setattr(app, "_LOG", recorder)
    monkeypatch.setattr(
        app,
        "configure_logging",
        lambda *args, **_kwargs: configured.append(args),
    )
    monkeypatch.setattr(app, "_print_runtime_settings", lambda *_args: None)
    monkeypatch.setenv("CAVEVIEWER_LOG_LEVEL", "INFO")
    return recorder, configured


def test_main_applies_update_branch_and_opens_cli_map(monkeypatch):
    recorder, configured = _prepare_main(monkeypatch)
    opened = []
    runtime = object()
    monkeypatch.setattr(
        app.sys,
        "argv",
        ["caveviewer", "--update-branch", "feature/updates", "/maps/cave"],
    )
    monkeypatch.setattr(
        platform_runtime_module,
        "create_platform_runtime",
        lambda **_kwargs: runtime,
    )
    monkeypatch.setattr(
        app,
        "_run_map_session",
        lambda path, **kwargs: opened.append((path, kwargs)),
    )

    app.main()

    assert configured == [("INFO",)]
    assert opened[0][0] == "/maps/cave"
    assert opened[0][1]["platform_runtime"] is runtime
    assert opened[0][1]["runtime_settings"]["update_branch"] == "feature/updates"
    assert app.os.environ.get("CAVEVIEWER_UPDATE_BRANCH") != "feature/updates"
    assert "Using update branch override: feature/updates" in recorder.info_messages


def test_main_consumes_cli_map_path_before_viewer_launch(monkeypatch):
    _recorder, _configured = _prepare_main(monkeypatch)
    opened = []
    runtime = object()
    monkeypatch.setattr(
        app.sys,
        "argv",
        ["caveviewer", "/maps/cave", "--backend", "glfw"],
    )
    monkeypatch.setattr(
        app,
        "_run_map_session",
        lambda path, **kwargs: opened.append((path, list(app.sys.argv), kwargs)),
    )
    monkeypatch.setattr(
        platform_runtime_module,
        "create_platform_runtime",
        lambda **_kwargs: runtime,
    )

    app.main()

    assert opened[0][0] == "/maps/cave"
    assert opened[0][1] == ["caveviewer", "--backend", "glfw"]
    assert opened[0][2]["platform_runtime"] is runtime
    assert opened[0][2]["runtime_settings"]["log_level"] == "INFO"


def test_main_rejects_invalid_update_branch_argument(monkeypatch, capsys):
    recorder, _configured = _prepare_main(monkeypatch)
    monkeypatch.setattr(app.sys, "argv", ["caveviewer", "--update-branch"])

    with pytest.raises(SystemExit) as raised:
        app.main()

    assert raised.value.code == 2
    assert "requires a non-empty branch name" in capsys.readouterr().err
    assert "requires a non-empty branch name" in recorder.error_messages[-1]


@pytest.mark.parametrize("dpi_fails", [False, True])
def test_main_configures_windows_dpi_best_effort(monkeypatch, dpi_fails):
    _recorder, _configured = _prepare_main(monkeypatch)
    calls = []
    dpi_utils = ModuleType("caveviewer.gui.dpi_utils")

    def configure_dpi():
        calls.append(True)
        if dpi_fails:
            raise RuntimeError("unsupported")

    dpi_utils.configure_process_dpi_awareness = configure_dpi
    monkeypatch.setitem(sys.modules, "caveviewer.gui.dpi_utils", dpi_utils)
    monkeypatch.setattr(app, "os", SimpleNamespace(name="nt", environ=os.environ))
    monkeypatch.setattr(app.sys, "argv", ["caveviewer", "/maps/cave"])
    monkeypatch.setattr(
        platform_runtime_module,
        "create_platform_runtime",
        lambda **_kwargs: object(),
    )
    monkeypatch.setattr(app, "_run_map_session", lambda _folder, **_kwargs: None)

    app.main()

    assert calls == [True]


def test_main_force_update_flag_configures_process_owned_manager(monkeypatch):
    recorder, _configured = _prepare_main(monkeypatch)
    splash_calls = []
    managers = _install_update_manager_module(monkeypatch)
    monkeypatch.setattr(app.sys, "argv", ["caveviewer", "--force-update"])
    _install_splash_module(
        monkeypatch,
        lambda **kwargs: splash_calls.append(kwargs) or None,
    )

    app.main()

    assert len(managers) == 1
    manager = managers[0]
    assert manager.current_version == "0.0.0"
    assert manager.platform_runtime is not None
    assert manager.shutdown_calls == 1
    assert len(splash_calls) == 1
    splash_call = splash_calls[0]
    assert splash_call["program_name"] == app.APP_NAME
    assert splash_call["version"] == "0.0.0"
    assert splash_call["update_manager"] is manager
    assert splash_call["desktop_services"] is manager.platform_runtime.desktop_services
    assert splash_call["platform_runtime"] is manager.platform_runtime
    assert splash_call["runtime_settings_provider"]()["force_update"] is True
    assert callable(splash_call["on_preferences_saved"])
    assert "--force-update" not in app.sys.argv
    assert recorder.info_messages[-1] == "No folder selected. Exiting."


def test_main_force_update_environment_exits_when_splash_is_closed(monkeypatch):
    recorder, _configured = _prepare_main(monkeypatch)
    versions = []
    monkeypatch.setenv("CAVEVIEWER_FORCE_UPDATE", "yes")
    monkeypatch.setattr(app.sys, "argv", ["caveviewer"])
    _install_splash_module(
        monkeypatch,
        lambda **kwargs: versions.append(kwargs["version"]) or None,
    )

    app.main()

    assert versions == ["0.0.0"]
    assert recorder.info_messages[-1] == "No folder selected. Exiting."


def test_preview_display_badge_is_not_used_for_update_version_comparison(
    monkeypatch,
):
    _recorder, _configured = _prepare_main(monkeypatch)
    managers = _install_update_manager_module(monkeypatch)
    splash_versions = []
    monkeypatch.setattr(app.sys, "argv", ["caveviewer"])
    monkeypatch.setattr(
        app,
        "current_runtime_platform_facts",
        lambda: RuntimePlatformFacts(
            platform_name="win32",
            os_name="nt",
            release_metadata=ReleaseMetadata(
                release_channel="preview",
                source=ReleaseMetadataSource.BUNDLED,
            ),
        ),
    )
    _install_splash_module(
        monkeypatch,
        lambda **kwargs: splash_versions.append(kwargs["version"]) or None,
    )

    app.main()

    assert splash_versions == [f"{app.__version__} Preview"]
    assert managers[0].current_version == app.__version__


def test_main_reopens_library_without_launch_overlay_after_map_session(monkeypatch):
    _recorder, _configured = _prepare_main(monkeypatch)
    selections = iter(("/maps/first", ""))
    versions = []
    seen_managers = []
    launch_overlays = []
    opened = []
    managers = _install_update_manager_module(monkeypatch)
    monkeypatch.setattr(app.sys, "argv", ["caveviewer", " "])
    monkeypatch.setattr(
        app,
        "_run_map_session",
        lambda folder, **kwargs: opened.append((folder, kwargs)),
    )
    _install_splash_module(
        monkeypatch,
        lambda **kwargs: (
            versions.append(kwargs["version"]),
            seen_managers.append(kwargs["update_manager"]),
            launch_overlays.append(kwargs["show_launch_overlay"]),
            next(selections),
        )[-1],
    )

    app.main()

    assert opened[0][0] == "/maps/first"
    assert opened[0][1]["platform_runtime"] is managers[0].platform_runtime
    assert opened[0][1]["runtime_settings"]["log_level"] == "INFO"
    assert versions == [app.__version__, app.__version__]
    assert managers == [seen_managers[0]]
    assert seen_managers == [managers[0], managers[0]]
    assert launch_overlays == [True, False]
    assert managers[0].shutdown_calls == 1


def test_run_returns_normally_when_main_succeeds(monkeypatch):
    called = []
    monkeypatch.setattr(app, "main", lambda: called.append(True))

    app.run()

    assert called == [True]


def test_app_forwards_session_events_to_active_diagnostics():
    calls = []

    class DiagnosticsRecorder:
        def record(self, event, **payload):
            calls.append(("event", event, payload))

        def record_exception(self, event, error_type, error, traceback, **context):
            calls.append(
                (
                    "exception",
                    event,
                    error_type,
                    str(error),
                    traceback,
                    context,
                )
            )

    diagnostics = DiagnosticsRecorder()
    previous = app.get_active_application_diagnostics()
    app.set_active_application_diagnostics(diagnostics)
    error = RuntimeError("OpenGL unavailable")
    try:
        app._record_application_event("viewer_launch_requested", cache_dir="/cache")
        app._record_application_exception(
            "viewer_launch_failed",
            error,
            cache_dir="/cache",
        )
    finally:
        app.set_active_application_diagnostics(previous)

    assert calls == [
        ("event", "viewer_launch_requested", {"cache_dir": "/cache"}),
        (
            "exception",
            "viewer_launch_failed",
            RuntimeError,
            "OpenGL unavailable",
            error.__traceback__,
            {"fatal": True, "cache_dir": "/cache"},
        ),
    ]


def test_runtime_diagnostics_attach_and_viewer_error_include_log_path(
    tmp_path,
    monkeypatch,
):
    calls = []
    dialog_calls = []

    class RuntimeDiagnosticsRecorder:
        def __init__(self):
            self.path = tmp_path / "viewer-session.log"

        def attach_to_root_logger(self):
            calls.append("attach")

        def enable_fault_handler(self):
            calls.append("fault_handler")

    class FakeRoot:
        def withdraw(self):
            dialog_calls.append("withdraw")

    def create_root(**options):
        dialog_calls.append(("root", options))
        return FakeRoot()

    def show_error(message, *, parent):
        dialog_calls.append(("error", message, parent))

    tkinter = ModuleType("tkinter")
    tkinter.Tk = create_root
    monkeypatch.setitem(sys.modules, "tkinter", tkinter)
    from caveviewer.gui import notifications

    monkeypatch.setattr(notifications, "show_error", show_error)

    diagnostics = RuntimeDiagnosticsRecorder()
    previous = app.get_active_runtime_diagnostics()
    app.set_active_runtime_diagnostics(diagnostics)
    try:
        app._attach_runtime_diagnostics_logging()
        app._show_viewer_launch_error(RuntimeError("OpenGL unavailable"))
    finally:
        app.set_active_runtime_diagnostics(previous)

    assert calls == ["attach", "fault_handler"]
    assert dialog_calls[0] == ("root", tk_root_options())
    assert dialog_calls[1] == "withdraw"
    assert dialog_calls[2][0] == "error"
    assert "OpenGL unavailable" in dialog_calls[2][1]
    assert str(diagnostics.path) in dialog_calls[2][1]
    assert dialog_calls[2][2].__class__ is FakeRoot


def test_runtime_diagnostics_records_attachment_failure():
    calls = []

    class RuntimeDiagnosticsRecorder:
        def attach_to_root_logger(self):
            raise RuntimeError("log path unavailable")

        def enable_fault_handler(self):
            raise AssertionError("fault handler must not be enabled after attach failure")

        def record_exception(self, event, error):
            calls.append((event, str(error)))

    diagnostics = RuntimeDiagnosticsRecorder()
    previous = app.get_active_runtime_diagnostics()
    app.set_active_runtime_diagnostics(diagnostics)
    try:
        app._attach_runtime_diagnostics_logging()
    finally:
        app.set_active_runtime_diagnostics(previous)

    assert calls == [("runtime_logging_attachment_failed", "log path unavailable")]


def test_show_viewer_launch_error_does_not_mask_dialog_failure(monkeypatch):
    tkinter = ModuleType("tkinter")

    def create_root(**_options):
        raise RuntimeError("no display")

    tkinter.Tk = create_root
    monkeypatch.setitem(sys.modules, "tkinter", tkinter)

    app._show_viewer_launch_error(RuntimeError("OpenGL unavailable"))


def test_run_binds_windows_runtime_diagnostics_to_application_events(
    tmp_path,
    monkeypatch,
):
    calls = []

    class RuntimeDiagnosticsRecorder:
        path = tmp_path / "viewer-session-test.log"
        jsonl_path = tmp_path / "viewer-session-test.jsonl"

        def record(self, stage, **context):
            calls.append(("record", stage, context))

        def close(self):
            calls.append(("close",))

    diagnostics = RuntimeDiagnosticsRecorder()
    monkeypatch.setattr(
        app,
        "create_runtime_diagnostics",
        lambda **_kwargs: diagnostics,
    )
    monkeypatch.setattr(app, "main", lambda: calls.append(("main",)))

    app.run()

    records = [
        json.loads(line)
        for line in diagnostics.jsonl_path.read_text(encoding="utf-8").splitlines()
    ]
    bound = next(
        record
        for record in records
        if record["event"] == "application_diagnostics_bound"
    )
    assert bound["runtime_log_path"] == str(diagnostics.path)
    assert calls[0][0:2] == ("record", "app_run_entered")
    assert ("main",) in calls
    assert calls[-1] == ("close",)


def test_run_uses_and_closes_optional_startup_diagnostics(monkeypatch):
    calls = []

    class StartupDiagnosticsRecorder:
        def record(self, stage, **context):
            calls.append(("record", stage, context))

        def attach_to_root_logger(self):
            calls.append(("attach",))

        def close(self):
            calls.append(("close",))

    diagnostics = StartupDiagnosticsRecorder()

    def run_main():
        calls.append(("main",))
        app._attach_startup_diagnostics_logging()

    monkeypatch.setattr(app, "main", run_main)

    app.run(startup_diagnostics=diagnostics)

    assert calls == [
        ("record", "app_run_entered", {}),
        ("main",),
        ("attach",),
        ("close",),
    ]


def test_run_exits_cleanly_on_keyboard_interrupt(monkeypatch):
    recorder = _LogRecorder()
    configured = []
    monkeypatch.setattr(app, "_LOG", recorder)
    monkeypatch.setattr(app, "configure_logging", lambda: configured.append(True))

    def interrupt():
        raise KeyboardInterrupt()

    monkeypatch.setattr(app, "main", interrupt)

    with pytest.raises(SystemExit) as raised:
        app.run()

    assert raised.value.code == 130
    assert configured == [True]
    assert recorder.info_messages == [f"{app.APP_NAME} interrupted by user."]
    assert recorder.error_messages == []


def test_run_finalizes_application_diagnostics_before_reraising_system_exit(
    monkeypatch,
):
    calls = []

    class ApplicationDiagnosticsRecorder:
        session_id = "session-test"

        def __init__(self, *, metadata):
            calls.append(("created", metadata))

        def install_hooks(self, *, install_signals):
            calls.append(("install_hooks", install_signals))

        def finalize(self, **outcome):
            calls.append(("finalize", outcome))

    def exit_main():
        raise SystemExit("restart requested")

    monkeypatch.setattr(app, "ApplicationDiagnostics", ApplicationDiagnosticsRecorder)
    monkeypatch.setattr(app, "create_runtime_diagnostics", lambda **_kwargs: None)
    monkeypatch.setattr(app, "main", exit_main)

    with pytest.raises(SystemExit) as raised:
        app.run()

    assert raised.value.code == "restart requested"
    assert ("install_hooks", True) in calls
    assert (
        "finalize",
        {
            "outcome": "system_exit",
            "exit_code": 1,
            "reason": "sys_exit",
        },
    ) in calls


@pytest.mark.parametrize(
    ("value", "expected"),
    [(None, 0), (7, 7), ("restart requested", 1)],
)
def test_system_exit_code_normalizes_supported_exit_values(value, expected):
    assert app._system_exit_code(value) == expected


@pytest.mark.parametrize("dialog_fails", [False, True])
def test_run_logs_fatal_error_and_uses_best_effort_dialog(monkeypatch, dialog_fails):
    recorder = _LogRecorder()
    configured = []
    dialog_calls = []
    root_options = []
    monkeypatch.setattr(app, "_LOG", recorder)
    monkeypatch.setattr(app, "configure_logging", lambda: configured.append(True))

    def fail_main():
        raise RuntimeError("startup exploded")

    monkeypatch.setattr(app, "main", fail_main)
    tkinter = ModuleType("tkinter")

    class FakeRoot:
        def withdraw(self):
            dialog_calls.append("withdraw")

    def create_root(**kwargs):
        root_options.append(kwargs)
        if dialog_fails:
            raise RuntimeError("no display")
        return FakeRoot()

    tkinter.Tk = create_root
    tkinter.messagebox = SimpleNamespace(
        showerror=lambda *args, **kwargs: dialog_calls.append((args, kwargs))
    )
    monkeypatch.setitem(sys.modules, "tkinter", tkinter)

    with pytest.raises(SystemExit) as raised:
        app.run()

    assert raised.value.code == 1
    assert configured == [True]
    assert "startup exploded" in recorder.error_messages[-1]
    assert "Traceback:" in recorder.error_messages[-1]
    assert root_options == [tk_root_options()]
    if dialog_fails:
        assert dialog_calls == []
    else:
        assert dialog_calls[0] == "withdraw"
        args, kwargs = dialog_calls[1]
        assert args[0] == app.APP_NAME
        assert "startup exploded" in args[1]
        assert "Traceback:" not in args[1]
        assert kwargs["parent"].__class__ is FakeRoot
