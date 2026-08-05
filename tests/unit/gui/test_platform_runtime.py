"""Test composition-time platform facts, update probes, and shared adapters."""

from __future__ import annotations

import pytest

from caveviewer.core.capabilities import (
    CapabilityResult,
    CapabilitySource,
    CapabilityStatus,
    DesktopNotificationRoute,
    DesktopNotificationTarget,
    DirectorySelectionRoute,
    DirectorySelectionTarget,
    FileSelectionRoute,
    FileSelectionTarget,
    IdleSuspendInhibitionRoute,
    IdleSuspendInhibitionTarget,
    UpdatePackageRevealRoute,
    ViewerLaunchRoute,
    ViewerLaunchTarget,
    WindowBackendPlan,
    WindowSystem,
)
from caveviewer.gui.features import FeatureDecision, FeatureId, FeatureState
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


class FakeUpdatePackageStorageAdapter:
    def persist_verified_package(self, _temporary_payload_path, _download_url):
        raise AssertionError("runtime composition must not persist a package")


class FakeSavedRecordingRevealAdapter:
    def reveal_saved_recording(self, _output_path):
        raise AssertionError("runtime composition must not reveal a recording")


class FakeRecordingProcessAdapter:
    def encoder_popen_kwargs(self):
        raise AssertionError("runtime composition must not start an encoder")


class FakeTlsTrustAdapter:
    def augment_ssl_context(self, _context):
        raise AssertionError("runtime composition must not create an SSL context")


class FakeWindowBackendAdapter:
    def launch_viewer(self, _target, _request):
        raise AssertionError("runtime composition must not launch a viewer")


def test_runtime_resolves_environment_only_when_it_is_composed(monkeypatch):
    monkeypatch.setenv("CAVEVIEWER_UPDATE_BRANCH", "ignored-process-value")
    adapter = FakeUpdateAdapter()
    desktop_services = object()
    storage_adapter = FakeUpdatePackageStorageAdapter()
    recording_reveal_adapter = FakeSavedRecordingRevealAdapter()
    recording_process_adapter = FakeRecordingProcessAdapter()
    tls_trust_adapter = FakeTlsTrustAdapter()
    window_backend_adapter = FakeWindowBackendAdapter()

    runtime = create_platform_runtime(
        platform_adapter=adapter,
        desktop_services=desktop_services,
        update_package_storage_adapter=storage_adapter,
        saved_recording_reveal_adapter=recording_reveal_adapter,
        recording_process_adapter=recording_process_adapter,
        tls_trust_adapter=tls_trust_adapter,
        window_backend_adapter=window_backend_adapter,
        environment={
            "CAVEVIEWER_UPDATE_BRANCH": "release-candidate",
            "CAVEVIEWER_UPDATE_CHANNEL": "prerelease",
        },
        platform_name="linux",
        machine="x86_64",
    )

    assert runtime.platform_adapter is adapter
    assert runtime.desktop_services is desktop_services
    assert runtime.update_package_storage_adapter is storage_adapter
    assert runtime.saved_recording_reveal_adapter is recording_reveal_adapter
    assert runtime.recording_process_adapter is recording_process_adapter
    assert runtime.tls_trust_adapter is tls_trust_adapter
    assert runtime.window_backend_adapter is window_backend_adapter
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
    assert FeatureId.GUIDED_DIVE_PLAYBACK not in platform_runtime.feature_gates.decisions

    preflight = platform_runtime.directory_selection_preflight()

    assert calls == [desktop_services]
    assert preflight.capability.value is target
    assert preflight.decision.feature is FeatureId.DIRECTORY_SELECTION
    assert preflight.decision.state is FeatureState.ENABLED
    assert preflight.decision.route == "portal_then_tk"


def test_directory_selection_preflight_rejects_a_route_that_disagrees_with_target():
    capability = CapabilityResult.available(
        DirectorySelectionTarget(DirectorySelectionRoute.TK),
        reason_code="directory_selection_tk_route_available",
    )
    decision = FeatureDecision(
        feature=FeatureId.DIRECTORY_SELECTION,
        state=FeatureState.ENABLED,
        reason_code="directory_selection_available",
        explanation="Directory selection is available.",
        route="portal_then_tk",
    )

    with pytest.raises(ValueError, match="must match its typed target"):
        runtime.DirectorySelectionPreflight(
            capability=capability,
            decision=decision,
        )


def test_runtime_keeps_file_selection_probe_on_demand(monkeypatch):
    calls = []
    desktop_services = object()
    target = FileSelectionTarget(
        primary_route=FileSelectionRoute.PORTAL,
        fallback_route=FileSelectionRoute.TK,
    )

    def probe(service):
        calls.append(service)
        return CapabilityResult.available(
            target,
            reason_code="file_selection_portal_route_available",
        )

    monkeypatch.setattr(runtime, "probe_file_selection", probe)
    platform_runtime = create_platform_runtime(
        platform_adapter=FakeUpdateAdapter(),
        desktop_services=desktop_services,
        environment={},
    )

    assert calls == []
    assert FeatureId.FILE_SELECTION not in platform_runtime.feature_gates.decisions

    preflight = platform_runtime.file_selection_preflight()

    assert calls == [desktop_services]
    assert preflight.capability.value is target
    assert preflight.decision.feature is FeatureId.FILE_SELECTION
    assert preflight.decision.state is FeatureState.ENABLED
    assert preflight.decision.route == "portal_then_tk"


def test_file_selection_preflight_rejects_a_route_that_disagrees_with_target():
    capability = CapabilityResult.available(
        FileSelectionTarget(FileSelectionRoute.TK),
        reason_code="file_selection_tk_route_available",
    )
    decision = FeatureDecision(
        feature=FeatureId.FILE_SELECTION,
        state=FeatureState.ENABLED,
        reason_code="file_selection_available",
        explanation="File selection is available.",
        route="portal_then_tk",
    )

    with pytest.raises(ValueError, match="must match its typed target"):
        runtime.FileSelectionPreflight(
            capability=capability,
            decision=decision,
        )


def test_runtime_keeps_desktop_notification_probe_on_demand(monkeypatch):
    calls = []
    desktop_services = object()
    target = DesktopNotificationTarget(
        primary_route=DesktopNotificationRoute.PORTAL,
        fallback_route=DesktopNotificationRoute.NOOP,
    )

    def probe(service):
        calls.append(service)
        return CapabilityResult.available(
            target,
            reason_code="desktop_notification_portal_route_available",
        )

    monkeypatch.setattr(runtime, "probe_desktop_notification", probe)
    platform_runtime = create_platform_runtime(
        platform_adapter=FakeUpdateAdapter(),
        desktop_services=desktop_services,
        environment={},
    )

    assert calls == []
    assert FeatureId.DESKTOP_NOTIFICATION not in platform_runtime.feature_gates.decisions

    preflight = platform_runtime.desktop_notification_preflight()

    assert calls == [desktop_services]
    assert preflight.capability.value is target
    assert preflight.decision.feature is FeatureId.DESKTOP_NOTIFICATION
    assert preflight.decision.state is FeatureState.ENABLED
    assert preflight.decision.route == "portal_then_noop"


def test_desktop_notification_preflight_rejects_a_route_that_disagrees_with_target():
    capability = CapabilityResult.available(
        DesktopNotificationTarget(DesktopNotificationRoute.INJECTED),
        reason_code="desktop_notification_injected_service_available",
    )
    decision = FeatureDecision(
        feature=FeatureId.DESKTOP_NOTIFICATION,
        state=FeatureState.ENABLED,
        reason_code="desktop_notification_available",
        explanation="Desktop notifications are available.",
        route="portal_then_noop",
    )

    with pytest.raises(ValueError, match="must match its typed target"):
        runtime.DesktopNotificationPreflight(
            capability=capability,
            decision=decision,
        )


def test_runtime_keeps_idle_suspend_inhibition_probe_on_demand(monkeypatch):
    calls = []
    desktop_services = object()
    target = IdleSuspendInhibitionTarget(
        primary_route=IdleSuspendInhibitionRoute.PORTAL,
        fallback_route=IdleSuspendInhibitionRoute.NOOP,
    )

    def probe(service):
        calls.append(service)
        return CapabilityResult.available(
            target,
            reason_code="idle_suspend_inhibition_portal_route_available",
        )

    monkeypatch.setattr(runtime, "probe_idle_suspend_inhibition", probe)
    platform_runtime = create_platform_runtime(
        platform_adapter=FakeUpdateAdapter(),
        desktop_services=desktop_services,
        environment={},
    )

    assert calls == []
    assert (
        FeatureId.IDLE_SUSPEND_INHIBITION
        not in platform_runtime.feature_gates.decisions
    )

    preflight = platform_runtime.idle_suspend_inhibition_preflight()

    assert calls == [desktop_services]
    assert preflight.capability.value is target
    assert preflight.decision.feature is FeatureId.IDLE_SUSPEND_INHIBITION
    assert preflight.decision.state is FeatureState.ENABLED
    assert preflight.decision.route == "portal_then_noop"


def test_idle_suspend_inhibition_preflight_rejects_route_target_disagreement():
    capability = CapabilityResult.available(
        IdleSuspendInhibitionTarget(IdleSuspendInhibitionRoute.INJECTED),
        reason_code="idle_suspend_inhibition_injected_service_available",
    )
    decision = FeatureDecision(
        feature=FeatureId.IDLE_SUSPEND_INHIBITION,
        state=FeatureState.ENABLED,
        reason_code="idle_suspend_inhibition_available",
        explanation="Desktop idle/suspend inhibition is available.",
        route="portal_then_noop",
    )

    with pytest.raises(ValueError, match="must match its typed target"):
        runtime.IdleSuspendInhibitionPreflight(
            capability=capability,
            decision=decision,
        )


def test_runtime_keeps_viewer_launch_probe_on_demand(monkeypatch):
    calls = []
    target = ViewerLaunchTarget(
        ViewerLaunchRoute.NATIVE_MODERNGL,
        WindowBackendPlan(WindowSystem.AUTO, ()),
    )

    def probe(*, platform_name=None):
        calls.append(platform_name)
        return CapabilityResult.available(
            target,
            reason_code="viewer_launch_native_route_available",
        )

    monkeypatch.setattr(runtime, "probe_viewer_launch", probe)
    platform_runtime = create_platform_runtime(
        platform_adapter=FakeUpdateAdapter(),
        desktop_services=object(),
        environment={},
        platform_name="darwin",
    )

    assert calls == []
    assert FeatureId.VIEWER_LAUNCH not in platform_runtime.feature_gates.decisions

    preflight = platform_runtime.viewer_launch_preflight()

    assert calls == ["darwin"]
    assert preflight.capability.value is target
    assert preflight.decision.feature is FeatureId.VIEWER_LAUNCH
    assert preflight.decision.state is FeatureState.ENABLED
    assert preflight.decision.route == "native_moderngl"


def test_viewer_launch_preflight_rejects_route_target_disagreement():
    capability = CapabilityResult.available(
        ViewerLaunchTarget(
            ViewerLaunchRoute.NATIVE_MODERNGL,
            WindowBackendPlan(WindowSystem.AUTO, ()),
        ),
        reason_code="viewer_launch_native_route_available",
    )
    decision = FeatureDecision(
        feature=FeatureId.VIEWER_LAUNCH,
        state=FeatureState.ENABLED,
        reason_code="viewer_launch_available",
        explanation="The viewer window is available.",
        route="glfw_moderngl:x11",
    )

    with pytest.raises(ValueError, match="must match its typed target"):
        runtime.ViewerLaunchPreflight(
            capability=capability,
            decision=decision,
        )


def test_linux_factory_shares_an_injected_desktop_service_with_its_adapter():
    desktop_services = object()

    adapter = get_platform_adapter(
        platform_name="linux",
        desktop_services=desktop_services,
    )

    assert isinstance(adapter, LinuxSplashPlatformAdapter)
    assert adapter._desktop_services is desktop_services
