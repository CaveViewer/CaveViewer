"""Test composition-time platform facts, update probes, and shared adapters."""

from __future__ import annotations

from caveviewer.core.capabilities import (
    CapabilityResult,
    CapabilitySource,
    CapabilityStatus,
    DirectorySelectionRoute,
    DirectorySelectionTarget,
    UpdatePackageRevealRoute,
)
from caveviewer.gui.features import FeatureId, FeatureState
from caveviewer.gui.platform import runtime
from caveviewer.gui.platform.factory import get_platform_adapter
from caveviewer.gui.platform.linux import LinuxSplashPlatformAdapter
from caveviewer.gui.platform.probes.recording import VideoRecordingTarget
from caveviewer.gui.platform.runtime import create_platform_runtime


class FakeUpdateAdapter:
    def __init__(self, *, supported: bool = True):
        self.supported = supported

    def default_update_repo(self):
        return "CaveViewer/CaveViewer"

    def default_update_manifest_url(self, repo, branch):
        return f"https://updates.example/{repo}/{branch}/stable.json"

    def install_channel(self):
        return "test_app"

    def supports_install_channel(self, channel):
        return self.supported and channel == "test_app"


class FailingUpdateConfigurationAdapter(FakeUpdateAdapter):
    def default_update_repo(self):
        raise RuntimeError("broken package metadata")


def test_runtime_resolves_environment_only_when_it_is_composed(monkeypatch):
    monkeypatch.setenv("CAVEVIEWER_UPDATE_BRANCH", "ignored-process-value")
    adapter = FakeUpdateAdapter()
    desktop_services = object()

    runtime = create_platform_runtime(
        platform_adapter=adapter,
        desktop_services=desktop_services,
        environment={
            "CAVEVIEWER_UPDATE_BRANCH": "release-candidate",
            "CAVEVIEWER_UPDATE_CHANNEL": "prerelease",
        },
        platform_name="linux",
        machine="x86_64",
    )

    assert runtime.platform_adapter is adapter
    assert runtime.desktop_services is desktop_services
    assert runtime.profile.platform_name == "linux"
    assert runtime.profile.machine == "x86_64"
    assert runtime.update_configuration.branch == "release-candidate"
    assert runtime.update_configuration.manifest_channel == "prerelease"
    assert runtime.update_configuration.manifest_url.endswith(
        "/release-candidate/prerelease.json"
    )
    assert runtime.update_configuration.source is CapabilitySource.USER_OVERRIDE
    assert runtime.automatic_update_capability.status is CapabilityStatus.AVAILABLE
    assert runtime.automatic_update_decision.state is FeatureState.ENABLED
    assert (
        runtime.static_feature_decision(FeatureId.AUTOMATIC_UPDATE)
        is runtime.automatic_update_decision
    )
    assert (
        runtime.update_package_reveal_capability.status
        is CapabilityStatus.AVAILABLE
    )
    assert (
        runtime.update_package_reveal_capability.value
        is UpdatePackageRevealRoute.DESKTOP_SERVICE
    )
    assert runtime.update_package_reveal_decision.state is FeatureState.ENABLED
    assert (
        runtime.static_feature_decision(FeatureId.UPDATE_PACKAGE_REVEAL)
        is runtime.update_package_reveal_decision
    )
    assert FeatureId.UPDATE_PACKAGE_REVEAL in runtime.feature_gates.decisions
    assert FeatureId.VIDEO_RECORDING not in runtime.feature_gates.decisions


def test_runtime_disables_unsupported_update_targets_before_network_work():
    runtime = create_platform_runtime(
        platform_adapter=FakeUpdateAdapter(supported=False),
        desktop_services=object(),
        environment={},
    )

    assert runtime.automatic_update_capability.status is CapabilityStatus.UNAVAILABLE
    assert (
        runtime.automatic_update_capability.reason_code
        == "automatic_update_target_unsupported"
    )
    assert runtime.automatic_update_decision.state is FeatureState.DISABLED


def test_runtime_disables_unsupported_update_package_reveal_routes():
    runtime = create_platform_runtime(
        platform_adapter=FakeUpdateAdapter(),
        desktop_services=object(),
        environment={},
        platform_name="freebsd",
    )

    assert (
        runtime.update_package_reveal_capability.status
        is CapabilityStatus.UNAVAILABLE
    )
    assert (
        runtime.update_package_reveal_capability.reason_code
        == "update_package_reveal_route_unsupported"
    )
    assert runtime.update_package_reveal_decision.state is FeatureState.DISABLED


def test_runtime_fails_closed_when_static_update_configuration_cannot_be_probed():
    runtime = create_platform_runtime(
        platform_adapter=FailingUpdateConfigurationAdapter(),
        desktop_services=object(),
        environment={},
    )

    assert runtime.automatic_update_capability.status is CapabilityStatus.UNKNOWN
    assert (
        runtime.automatic_update_capability.reason_code
        == "automatic_update_configuration_probe_failed"
    )
    assert runtime.automatic_update_decision.state is FeatureState.DISABLED


def test_runtime_keeps_video_recording_probe_on_demand(monkeypatch):
    calls = []

    def probe(output_directory, *, ffmpeg_resolver=None):
        calls.append((output_directory, ffmpeg_resolver))
        return CapabilityResult.available(
            VideoRecordingTarget("/usr/bin/ffmpeg", output_directory),
            reason_code="video_recording_target_available",
        )

    monkeypatch.setattr(runtime, "probe_video_recording", probe)

    platform_runtime = create_platform_runtime(
        platform_adapter=FakeUpdateAdapter(),
        desktop_services=object(),
        environment={},
    )

    assert calls == []
    preflight = platform_runtime.video_recording_preflight(
        "/recordings",
        ffmpeg_resolver=lambda: "/usr/bin/ffmpeg",
    )

    assert calls[0][0] == "/recordings"
    assert len(calls) == 1
    assert preflight.capability.value == VideoRecordingTarget(
        "/usr/bin/ffmpeg", "/recordings"
    )
    assert preflight.decision.state is FeatureState.ENABLED
    assert preflight.decision.route == "ffmpeg"


def test_runtime_keeps_directory_selection_probe_on_demand(monkeypatch):
    calls = []
    desktop_services = object()
    target = DirectorySelectionTarget(
        primary_route=DirectorySelectionRoute.PORTAL,
        fallback_route=DirectorySelectionRoute.TK,
    )

    def probe(service):
        calls.append(service)
        return CapabilityResult.available(
            target,
            reason_code="directory_selection_portal_route_available",
        )

    monkeypatch.setattr(runtime, "probe_directory_selection", probe)
    platform_runtime = create_platform_runtime(
        platform_adapter=FakeUpdateAdapter(),
        desktop_services=desktop_services,
        environment={},
    )

    assert calls == []
    assert FeatureId.DIRECTORY_SELECTION not in platform_runtime.feature_gates.decisions

    preflight = platform_runtime.directory_selection_preflight()

    assert calls == [desktop_services]
    assert preflight.capability.value is target
    assert preflight.decision.feature is FeatureId.DIRECTORY_SELECTION
    assert preflight.decision.state is FeatureState.ENABLED
    assert preflight.decision.route == "portal_then_tk"


def test_linux_factory_shares_an_injected_desktop_service_with_its_adapter():
    desktop_services = object()

    adapter = get_platform_adapter(
        platform_name="linux",
        desktop_services=desktop_services,
    )

    assert isinstance(adapter, LinuxSplashPlatformAdapter)
    assert adapter._desktop_services is desktop_services
