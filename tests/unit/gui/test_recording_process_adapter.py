"""Test direct platform configuration for recording encoder startup."""

from __future__ import annotations

from caveviewer.gui.platform import recording_process


def test_default_process_adapter_has_no_platform_launch_options():
    adapter = recording_process.create_recording_process_adapter(
        platform_name="linux"
    )

    assert adapter.encoder_popen_kwargs() == {}


def test_windows_process_adapter_suppresses_console(monkeypatch):
    class FakeStartupInfo:
        def __init__(self):
            self.dwFlags = 2
            self.wShowWindow = 99

    monkeypatch.setattr(recording_process.subprocess, "STARTUPINFO", FakeStartupInfo, raising=False)
    monkeypatch.setattr(recording_process.subprocess, "STARTF_USESHOWWINDOW", 4, raising=False)
    monkeypatch.setattr(recording_process.subprocess, "CREATE_NO_WINDOW", 8, raising=False)

    adapter = recording_process.create_recording_process_adapter(
        platform_name="win32"
    )
    options = adapter.encoder_popen_kwargs()

    assert options["startupinfo"].dwFlags == 6
    assert options["startupinfo"].wShowWindow == 0
    assert options["creationflags"] == 8
