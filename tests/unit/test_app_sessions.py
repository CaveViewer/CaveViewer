"""Tests for CaveViewer map-session and top-level application control flow."""

from __future__ import annotations

import sys
import traceback
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from caveviewer import app
from caveviewer.core import chunker
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
        def __init__(self, current_version):
            self.current_version = current_version
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

    assert opened == [((str(tmp_path),), {"textures_dir": str(tmp_path)})]


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
    monkeypatch.setattr(app, "_LOG", recorder)
    monkeypatch.setattr(traceback, "print_exc", lambda: printed.append(True))
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


def test_map_session_opens_obj_with_existing_cache(tmp_path, monkeypatch):
    source = tmp_path / "map.obj"
    material = tmp_path / "map.mtl"
    cache_dir = tmp_path / "managed-cache"
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

    assert opened == [((str(cache_dir),), {"textures_dir": str(cache_dir)})]


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


def test_map_session_rejects_direct_unsupported_file(tmp_path, monkeypatch):
    payload = tmp_path / "notes.txt"
    payload.write_text("not a cave map", encoding="utf-8")
    recorder = _LogRecorder()
    monkeypatch.setattr(app, "_LOG", recorder)

    with pytest.raises(SystemExit) as raised:
        app._run_map_session(str(payload))

    assert raised.value.code == 1
    assert "No supported model file" in recorder.error_messages[-1]


@pytest.mark.parametrize("cache_is_valid", [True, False])
def test_map_session_reports_source_viewer_failures(
    tmp_path, monkeypatch, cache_is_valid
):
    descriptor = {"format": "glb", "glb_path": str(tmp_path / "map.glb")}
    recorder = _LogRecorder()
    printed = []
    monkeypatch.setattr(app, "_LOG", recorder)
    monkeypatch.setattr(app, "find_model_file", lambda _folder: descriptor)
    monkeypatch.setattr(chunker, "cache_is_valid", lambda _path: cache_is_valid)
    monkeypatch.setattr(chunker, "get_cache_dir", lambda _path: "/cache/map")
    monkeypatch.setattr(chunker, "cache_chunk_size", lambda _path: 8.0)
    monkeypatch.setattr(chunker, "configured_chunk_size", lambda: 8.0)
    monkeypatch.setattr(traceback, "print_exc", lambda: printed.append(True))

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


def _prepare_main(monkeypatch):
    recorder = _LogRecorder()
    configured = []
    monkeypatch.setattr(app, "_LOG", recorder)
    monkeypatch.setattr(app, "configure_logging", lambda: configured.append(True))
    monkeypatch.setattr(app, "_print_caveviewer_environment_settings", lambda: None)
    return recorder, configured


def test_main_applies_update_branch_and_opens_cli_map(monkeypatch):
    recorder, configured = _prepare_main(monkeypatch)
    opened = []
    monkeypatch.setattr(
        app.sys,
        "argv",
        ["caveviewer", "--update-branch", "feature/updates", "/maps/cave"],
    )
    monkeypatch.setattr(app, "_run_map_session", opened.append)

    app.main()

    assert configured == [True]
    assert opened == ["/maps/cave"]
    assert app.os.environ["CAVEVIEWER_UPDATE_BRANCH"] == "feature/updates"
    assert "Using update branch override: feature/updates" in recorder.info_messages


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
    monkeypatch.setattr(app.os, "name", "nt")
    monkeypatch.setattr(app.sys, "argv", ["caveviewer", "/maps/cave"])
    monkeypatch.setattr(app, "_run_map_session", lambda _folder: None)

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
    assert manager.shutdown_calls == 1
    assert splash_calls == [
        {
            "program_name": app.APP_NAME,
            "version": "0.0.0",
            "update_manager": manager,
        }
    ]
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


def test_main_reopens_splash_after_a_map_session(monkeypatch):
    _recorder, _configured = _prepare_main(monkeypatch)
    selections = iter(("/maps/first", ""))
    versions = []
    seen_managers = []
    opened = []
    managers = _install_update_manager_module(monkeypatch)
    monkeypatch.setattr(app.sys, "argv", ["caveviewer", " "])
    monkeypatch.setattr(app, "_run_map_session", opened.append)
    _install_splash_module(
        monkeypatch,
        lambda **kwargs: (
            versions.append(kwargs["version"]),
            seen_managers.append(kwargs["update_manager"]),
            next(selections),
        )[-1],
    )

    app.main()

    assert opened == ["/maps/first"]
    assert versions == [app.__version__, app.__version__]
    assert managers == [seen_managers[0]]
    assert seen_managers == [managers[0], managers[0]]
    assert managers[0].shutdown_calls == 1


def test_run_returns_normally_when_main_succeeds(monkeypatch):
    called = []
    monkeypatch.setattr(app, "main", lambda: called.append(True))

    app.run()

    assert called == [True]


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
