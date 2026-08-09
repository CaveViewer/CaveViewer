"""Test the shared typed video-recording preflight boundary."""

from __future__ import annotations

from caveviewer.core.capabilities import CapabilityResult
from caveviewer.gui.features import FeatureState
from caveviewer.gui.platform import recording_preflight
from caveviewer.gui.platform.probes.recording import VideoRecordingTarget
from caveviewer.gui.platform.runtime import VideoRecordingPreflight


def test_video_recording_preflight_builds_the_compatibility_pair(monkeypatch):
    target = VideoRecordingTarget("/usr/bin/ffmpeg", "/recordings")
    capability = CapabilityResult.available(
        target,
        reason_code="video_recording_target_available",
    )
    calls = []

    def probe(output_directory, *, ffmpeg_resolver=None):
        calls.append((output_directory, ffmpeg_resolver))
        return capability

    monkeypatch.setattr(recording_preflight, "probe_video_recording", probe)

    preflight = recording_preflight.video_recording_preflight(
        "/recordings",
        ffmpeg_resolver=lambda: "/usr/bin/ffmpeg",
    )

    assert isinstance(preflight, VideoRecordingPreflight)
    assert preflight.capability is capability
    assert preflight.decision.state is FeatureState.ENABLED
    assert preflight.decision.route == target.route_key
    assert calls[0][0] == "/recordings"
    assert calls[0][1] is not None


def test_video_recording_preflight_uses_the_injected_runtime(monkeypatch):
    expected = object()
    calls = []

    class Runtime:
        def video_recording_preflight(self, output_directory, *, ffmpeg_resolver=None):
            calls.append((output_directory, ffmpeg_resolver))
            return expected

    def fail_probe(*_args, **_kwargs):
        raise AssertionError("the runtime preflight must replace the direct probe")

    monkeypatch.setattr(recording_preflight, "probe_video_recording", fail_probe)

    result = recording_preflight.video_recording_preflight(
        "/recordings",
        ffmpeg_resolver=lambda: "/usr/bin/ffmpeg",
        platform_runtime=Runtime(),
    )

    assert result is expected
    assert calls[0][0] == "/recordings"
    assert calls[0][1] is not None
