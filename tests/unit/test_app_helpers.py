"""Unit tests for CaveViewer application helpers and diagnostics."""

from __future__ import annotations

import io
import runpy
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from caveviewer import app
from caveviewer.core import chunker


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


def test_console_helpers_write_flush_and_newline(monkeypatch):
    stream = io.StringIO()
    monkeypatch.setattr(app.sys, "stdout", stream)

    app._console_write("status")
    app._console_newline()

    assert stream.getvalue() == "status\n"


def test_console_write_tolerates_missing_or_broken_stdout(monkeypatch):
    monkeypatch.setattr(app.sys, "stdout", None)
    app._console_write("ignored")

    class BrokenStream:
        def write(self, _text):
            raise OSError("console closed")

        def flush(self):
            raise AssertionError("flush must not run after a failed write")

    monkeypatch.setattr(app.sys, "stdout", BrokenStream())
    app._console_write("ignored")


def test_environment_default_helpers_cover_static_callable_and_failure(monkeypatch):
    assert app._default_io_workers() == "2"
    assert app._default_chunk_build_workers() == "1"
    assert app._effective_env_default("CAVEVIEWER_CHUNK_SIZE_METERS") == "50"
    assert app._effective_env_default("CAVEVIEWER_MAX_UPLOAD_GROUP_MB") == "16"
    assert app._effective_env_default("CAVEVIEWER_UI_TEXT_SCALE") == "1.28"
    assert app._effective_env_default("CAVEVIEWER_VIEWER_UI_SCALE") == "auto"
    assert app._effective_env_default("NOT_CONFIGURED") is None

    def fail_default():
        raise RuntimeError("unavailable")

    monkeypatch.setitem(app._CAVEVIEWER_ENV_EFFECTIVE_DEFAULTS, "BROKEN", fail_default)
    assert app._effective_env_default("BROKEN") is None


def test_environment_diagnostics_report_set_discovered_and_effective_values(
    monkeypatch,
):
    recorder = _LogRecorder()
    monkeypatch.setattr(app, "_LOG", recorder)
    monkeypatch.setenv("CAVEVIEWER_UI_TEXT_SCALE", "2.0")
    monkeypatch.setenv("CAVEVIEWER_CUSTOM_SETTING", "enabled")
    monkeypatch.setenv("CAVEVIEWER_EMPTY_SETTING", "")

    app._print_caveviewer_environment_settings()

    output = "\n".join(recorder.info_messages)
    assert "CAVEVIEWER_UI_TEXT_SCALE=2.0" in output
    assert "CAVEVIEWER_CUSTOM_SETTING=enabled" in output
    assert "CAVEVIEWER_IO_WORKERS=<unset> (effective: 2)" in output
    assert "CAVEVIEWER_EMPTY_SETTING" not in output


@pytest.mark.parametrize(
    ("argv", "expected_argv", "expected_branch"),
    [
        (["caveviewer"], ["caveviewer"], None),
        (
            ["caveviewer", "--update-branch", " release/test ", "map"],
            ["caveviewer", "map"],
            "release/test",
        ),
        (
            ["caveviewer", "--update-branch=feature/test", "map"],
            ["caveviewer", "map"],
            "feature/test",
        ),
    ],
)
def test_consume_update_branch_arg(argv, expected_argv, expected_branch):
    assert app._consume_update_branch_arg(argv) == (expected_argv, expected_branch)


@pytest.mark.parametrize(
    "argv",
    [
        ["caveviewer", "--update-branch"],
        ["caveviewer", "--update-branch", "  "],
        ["caveviewer", "--update-branch="],
    ],
)
def test_consume_update_branch_arg_rejects_missing_values(argv):
    with pytest.raises(ValueError, match="requires a non-empty branch name"):
        app._consume_update_branch_arg(argv)


@pytest.mark.parametrize(
    ("header", "extension"),
    [
        (b"\xff\xd8jpeg", ".jpg"),
        (b"\x89PNG\r\n\x1a\npng", ".png"),
        (b"unknown", ".img"),
    ],
)
def test_embedded_texture_filename_sniffs_image_format(header, extension):
    filename = app._embedded_texture_filename(header, "limestone")
    assert filename == f"limestone{extension}"


@pytest.mark.parametrize(("selection", "expected"), [("/maps/cave", "/maps/cave"), ("", None)])
def test_pick_folder_dialog_applies_scaling_and_cleans_up(
    monkeypatch, selection, expected
):
    calls = []

    class FakeRoot:
        def withdraw(self):
            calls.append("withdraw")

        def destroy(self):
            calls.append("destroy")

    root = FakeRoot()
    tkinter = ModuleType("tkinter")
    tkinter.Tk = lambda **kwargs: calls.append(("Tk", kwargs)) or root
    dpi_utils = ModuleType("caveviewer.gui.dpi_utils")
    dpi_utils.configure_process_dpi_awareness = lambda: calls.append("dpi")
    dpi_utils.apply_tk_scaling = lambda selected_root: calls.append(
        ("scale", selected_root)
    )
    monkeypatch.setitem(sys.modules, "tkinter", tkinter)
    monkeypatch.setitem(sys.modules, "caveviewer.gui.dpi_utils", dpi_utils)

    class FakeDesktopServices:
        def choose_directory(self, **options):
            calls.append(("choose_directory", options))
            if not selection:
                return None
            return SimpleNamespace(path=selection)

    assert app.pick_folder_dialog(desktop_services=FakeDesktopServices()) == expected
    assert calls[0] == "dpi"
    assert ("scale", root) in calls
    assert (
        "choose_directory",
        {"title": "Open Map Folder", "parent": root},
    ) in calls
    assert calls[-1] == "destroy"


def test_viewer_control_messages_are_logged(monkeypatch):
    recorder = _LogRecorder()
    monkeypatch.setattr(app, "_LOG", recorder)

    app._print_viewer_controls()

    assert recorder.info_messages == [
        "Launching viewer...",
        "Controls help is available in-app via the Help button.",
    ]


def test_cache_chunk_size_logging_handles_missing_matching_and_different_values(
    monkeypatch,
):
    recorder = _LogRecorder()
    monkeypatch.setattr(app, "_LOG", recorder)

    monkeypatch.setattr(chunker, "cache_chunk_size", lambda _path: None)
    monkeypatch.setattr(chunker, "configured_chunk_size", lambda: 8.0)
    app._log_cache_chunk_size("cache", context="Prebuilt")
    assert "does not report a valid chunk size" in recorder.warning_messages[-1]

    monkeypatch.setattr(chunker, "cache_chunk_size", lambda _path: 8.0)
    app._log_cache_chunk_size("cache")
    assert recorder.info_messages[-1] == "Chunk cache chunk size: 8."

    monkeypatch.setattr(chunker, "cache_chunk_size", lambda _path: 16.0)
    app._log_cache_chunk_size("cache")
    assert "existing/prebuilt caches always open" in recorder.info_messages[-1]


def test_app_import_tolerates_truststore_injection_failure(monkeypatch):
    truststore = ModuleType("truststore")

    def fail_injection():
        raise RuntimeError("unsupported")

    truststore.inject_into_ssl = fail_injection
    monkeypatch.setitem(sys.modules, "truststore", truststore)

    runpy.run_path(str(Path(app.__file__)), run_name="caveviewer_app_probe")
