"""Tests for durable cross-platform viewer-session diagnostics."""

from __future__ import annotations

import logging
import os

from caveviewer.core.diagnostics import runtime


class _FaultHandler:
    def __init__(self) -> None:
        self.enable_calls = []
        self.disable_calls = 0

    def enable(self, *, file, all_threads) -> None:
        self.enable_calls.append(
            {
                "file": file,
                "all_threads": all_threads,
            }
        )

    def disable(self) -> None:
        self.disable_calls += 1


class _PreEnabledFaultHandler:
    def is_enabled(self) -> bool:
        return True

    def enable(self, *, file, all_threads) -> None:
        raise AssertionError("an existing fault handler must not be replaced")


def test_windows_runtime_diagnostics_persist_runtime_and_fault_logs(tmp_path):
    path = tmp_path / "diagnostics" / "viewer-session-test.log"
    fault_handler = _FaultHandler()
    diagnostics = runtime.create_runtime_diagnostics(
        platform_name="win32",
        path=path,
        session_id="session-test",
        fault_handler=fault_handler,
    )

    assert diagnostics is not None
    diagnostics.attach_to_root_logger()
    assert diagnostics.enable_fault_handler() is True
    logging.getLogger("caveviewer.viewer").warning("native window reported a warning")
    try:
        raise RuntimeError("context creation failed")
    except RuntimeError as error:
        diagnostics.record_exception("viewer_window_config_create_failed", error)
    diagnostics.close()

    output = path.read_text(encoding="utf-8")
    assert "stage=runtime_diagnostics_created" in output
    assert "stage=application_logging_attached" in output
    assert "stage=fault_handler_enabled all_threads=True" in output
    assert "native window reported a warning" in output
    assert "stage=viewer_window_config_create_failed" in output
    assert "RuntimeError: context creation failed" in output
    assert "stage=runtime_diagnostics_closing" in output
    assert len(fault_handler.enable_calls) == 1
    assert fault_handler.enable_calls[0]["all_threads"] is True
    assert fault_handler.disable_calls == 1


def test_runtime_diagnostics_are_created_on_supported_desktops(tmp_path):
    diagnostics = runtime.create_runtime_diagnostics(
        platform_name="linux",
        path=tmp_path / "viewer.log",
        session_id="session-test",
        fault_handler=_FaultHandler(),
    )

    assert diagnostics is not None
    diagnostics.close()
    assert (tmp_path / "viewer.log").is_file()


def test_runtime_diagnostics_prune_old_session_logs_and_jsonl(tmp_path):
    diagnostics_directory = tmp_path / "diagnostics"
    diagnostics_directory.mkdir()
    for index in range(4):
        text_log = diagnostics_directory / f"viewer-session-{index}.log"
        text_log.write_text(str(index), encoding="utf-8")
        text_log.with_suffix(".jsonl").write_text(str(index), encoding="utf-8")

    newest_path = diagnostics_directory / "viewer-session-current.log"
    diagnostics = runtime.create_runtime_diagnostics(
        platform_name="darwin",
        path=newest_path,
        session_id="current",
        fault_handler=_FaultHandler(),
        retained_session_logs=2,
    )

    assert diagnostics is not None
    diagnostics.close()
    remaining = sorted(path.name for path in diagnostics_directory.glob("*.log"))
    assert remaining == ["viewer-session-3.log", "viewer-session-current.log"]
    assert not (diagnostics_directory / "viewer-session-0.jsonl").exists()
    assert not (diagnostics_directory / "viewer-session-1.jsonl").exists()
    assert not (diagnostics_directory / "viewer-session-2.jsonl").exists()


def test_runtime_diagnostics_expire_session_logs_older_than_one_day(tmp_path):
    diagnostics_directory = tmp_path / "diagnostics"
    diagnostics_directory.mkdir()
    expired_log = diagnostics_directory / "viewer-session-expired.log"
    expired_log.write_text("expired", encoding="utf-8")
    expired_log.with_suffix(".jsonl").write_text("expired", encoding="utf-8")
    os.utime(expired_log, (1, 1))
    os.utime(expired_log.with_suffix(".jsonl"), (1, 1))

    current_path = diagnostics_directory / "viewer-session-current.log"
    diagnostics = runtime.create_runtime_diagnostics(
        platform_name="win32",
        path=current_path,
        session_id="current",
        fault_handler=_FaultHandler(),
    )

    assert diagnostics is not None
    diagnostics.close()
    assert not expired_log.exists()
    assert not expired_log.with_suffix(".jsonl").exists()
    assert current_path.exists()


def test_runtime_diagnostics_preserve_an_existing_fault_handler(tmp_path):
    path = tmp_path / "viewer.log"
    diagnostics = runtime.RuntimeDiagnostics(
        path,
        fault_handler=_PreEnabledFaultHandler(),
    )

    assert diagnostics.enable_fault_handler() is False
    diagnostics.close()

    output = path.read_text(encoding="utf-8")
    assert "stage=fault_handler_already_enabled" in output


def test_active_runtime_diagnostics_records_viewer_checkpoints(tmp_path):
    path = tmp_path / "viewer.log"
    diagnostics = runtime.RuntimeDiagnostics(path, fault_handler=_FaultHandler())
    runtime.set_active_runtime_diagnostics(diagnostics)
    try:
        assert runtime.record_runtime_stage(
            "viewer_native_launch_begin",
            requested_window_size=(1600, 1000),
        )
        try:
            raise ValueError("window backend unavailable")
        except ValueError as error:
            assert runtime.record_runtime_exception(
                "viewer_native_launch_failed",
                error,
            )
    finally:
        runtime.set_active_runtime_diagnostics(None)
        diagnostics.close()

    output = path.read_text(encoding="utf-8")
    assert "stage=viewer_native_launch_begin" in output
    assert "stage=viewer_native_launch_failed" in output
    assert "ValueError: window backend unavailable" in output
