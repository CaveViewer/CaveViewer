"""Test on-demand ffmpeg and recording-destination capability probes."""

from __future__ import annotations

from caveviewer.core.capabilities import (
    CapabilityResult,
    CapabilitySource,
    CapabilityStatus,
)
from caveviewer.gui.platform.probes.recording import (
    probe_recording_output_directory,
    probe_video_recording,
)


def test_video_recording_probe_skips_the_output_folder_without_ffmpeg():
    output_probe_calls = []

    result = probe_video_recording(
        "/recordings",
        ffmpeg_resolver=lambda: None,
        output_directory_probe=lambda directory: output_probe_calls.append(directory),
    )

    assert result.status is CapabilityStatus.UNAVAILABLE
    assert result.reason_code == "video_recording_encoder_unavailable"
    assert output_probe_calls == []


def test_video_recording_probe_combines_encoder_and_writable_destination():
    result = probe_video_recording(
        "/recordings",
        ffmpeg_resolver=lambda: " /usr/bin/ffmpeg ",
        output_directory_probe=lambda _directory: CapabilityResult.available(
            "/recordings",
            reason_code="video_recording_output_directory_available",
        ),
        environment={"CAVEVIEWER_FFMPEG": "/usr/bin/ffmpeg"},
    )

    assert result.status is CapabilityStatus.AVAILABLE
    assert result.source is CapabilitySource.USER_OVERRIDE
    assert result.value is not None
    assert result.value.ffmpeg_path == "/usr/bin/ffmpeg"
    assert result.value.output_directory == "/recordings"


def test_video_recording_probe_propagates_an_unwritable_destination():
    result = probe_video_recording(
        "/recordings",
        ffmpeg_resolver=lambda: "/usr/bin/ffmpeg",
        output_directory_probe=lambda _directory: CapabilityResult.unavailable(
            reason_code="video_recording_output_directory_unavailable",
        ),
        environment={},
    )

    assert result.status is CapabilityStatus.UNAVAILABLE
    assert result.reason_code == "video_recording_output_directory_unavailable"


def test_recording_output_probe_leaves_no_preflight_file(tmp_path):
    output_directory = tmp_path / "videos"

    result = probe_recording_output_directory(str(output_directory))

    assert result.status is CapabilityStatus.AVAILABLE
    assert result.value == str(output_directory)
    assert list(output_directory.iterdir()) == []
