"""Test the focused post-save recording-reveal adapter."""

from __future__ import annotations

from caveviewer.gui.platform.saved_recording_reveal import (
    create_saved_recording_reveal_adapter,
)


class FakePlatformAdapter:
    def __init__(self):
        self.revealed_paths = []

    def reveal_file(self, output_path):
        self.revealed_paths.append(output_path)


def test_composed_recording_reveal_adapter_delegates_to_platform_behavior():
    platform_adapter = FakePlatformAdapter()
    reveal_adapter = create_saved_recording_reveal_adapter(platform_adapter)

    reveal_adapter.reveal_saved_recording("/recordings/cave.mp4")

    assert platform_adapter.revealed_paths == ["/recordings/cave.mp4"]
