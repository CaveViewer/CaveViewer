"""Tests for bounded Windows pre-splash diagnostics."""

from __future__ import annotations

import logging

from caveviewer.core.diagnostics import startup


class _FaultHandler:
    def __init__(self) -> None:
        self.dump_calls = []
        self.cancel_calls = 0

    def dump_traceback_later(self, timeout, *, repeat, file, exit) -> None:
        self.dump_calls.append(
            {
                "timeout": timeout,
                "repeat": repeat,
                "file": file,
                "exit": exit,
            }
        )

    def cancel_dump_traceback_later(self) -> None:
        self.cancel_calls += 1


def test_windows_startup_diagnostics_records_checkpoints_and_cancels_watchdog(
    tmp_path,
):
    path = tmp_path / "diagnostics" / "startup.log"
    fault_handler = _FaultHandler()

    diagnostics = startup.create_startup_diagnostics(
        platform_name="win32",
        path=path,
        watchdog_seconds=12,
        fault_handler=fault_handler,
    )

    assert diagnostics is not None
    diagnostics.record("app_import_complete")
    diagnostics.mark_splash_visible()

    output = path.read_text(encoding="utf-8")
    assert "stage=bootstrap_started" in output
    assert "stage=watchdog_armed timeout_seconds=12.0" in output
    assert "stage=app_import_complete" in output
    assert "stage=splash_visible" in output
    assert len(fault_handler.dump_calls) == 1
    assert fault_handler.dump_calls[0]["timeout"] == 12.0
    assert fault_handler.dump_calls[0]["repeat"] is False
    assert fault_handler.dump_calls[0]["exit"] is False
    assert fault_handler.cancel_calls == 1


def test_startup_diagnostics_attaches_application_logs_only_until_splash_is_visible(
    tmp_path,
):
    path = tmp_path / "startup.log"
    diagnostics = startup.StartupDiagnostics(path, fault_handler=_FaultHandler())
    root_logger = logging.getLogger()
    previous_handlers = list(root_logger.handlers)
    previous_level = root_logger.level
    root_logger.handlers.clear()
    root_logger.setLevel(logging.DEBUG)
    try:
        diagnostics.attach_to_root_logger()
        logging.getLogger("caveviewer.test").warning("before splash")
        diagnostics.mark_splash_visible()
        logging.getLogger("caveviewer.test").warning("after splash")
    finally:
        diagnostics.close()
        root_logger.handlers.clear()
        root_logger.handlers.extend(previous_handlers)
        root_logger.setLevel(previous_level)

    output = path.read_text(encoding="utf-8")
    assert "before splash" in output
    assert "after splash" not in output


def test_startup_diagnostics_are_windows_only(tmp_path):
    path = tmp_path / "startup.log"

    diagnostics = startup.create_startup_diagnostics(
        platform_name="linux",
        path=path,
    )

    assert diagnostics is None
    assert not path.exists()
