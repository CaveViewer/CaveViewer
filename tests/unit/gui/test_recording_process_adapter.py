"""Test the focused platform adapter for recording encoder process startup."""

from __future__ import annotations

from caveviewer.gui.platform.recording_process import (
    create_recording_process_adapter,
)


class FakePlatformAdapter:
    def __init__(self):
        self.calls = 0
        self.startup_kwargs = {"creationflags": 17}

    def recording_subprocess_startup_kwargs(self):
        self.calls += 1
        return self.startup_kwargs


def test_composed_process_adapter_delegates_encoder_popen_options():
    platform_adapter = FakePlatformAdapter()
    process_adapter = create_recording_process_adapter(platform_adapter)

    startup_kwargs = process_adapter.encoder_popen_kwargs()

    assert startup_kwargs == {"creationflags": 17}
    assert platform_adapter.calls == 1
