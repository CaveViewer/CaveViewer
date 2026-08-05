"""Test file-selection route facts without contacting desktop services."""

from __future__ import annotations

import pytest

from caveviewer.core.capabilities import (
    CapabilitySource,
    CapabilityStatus,
    FileSelectionRoute,
    FileSelectionTarget,
)
from caveviewer.gui.platform.desktop_services import (
    DesktopServiceError,
    TkDesktopServices,
)
from caveviewer.gui.platform.file_selection import (
    authorized_file_selection_target,
    choose_authorized_file,
    file_selection_preflight,
)
from caveviewer.gui.platform.portal import LinuxPortalDesktopServices
from caveviewer.gui.platform.probes.desktop import probe_file_selection


def test_file_selection_probe_reports_tk_without_creating_a_tk_root():
    result = probe_file_selection(TkDesktopServices())

    assert result.status is CapabilityStatus.AVAILABLE
    assert result.value == FileSelectionTarget(FileSelectionRoute.TK)
    assert result.reason_code == "file_selection_tk_route_available"
    assert result.source is CapabilitySource.DETECTED


def test_file_selection_probe_declares_portal_then_tk_without_transport_work():
    class UnexpectedPortalUse:
        def __getattr__(self, _name):
            pytest.fail("declaring a picker route must not contact the portal")

    services = LinuxPortalDesktopServices(
        portal=UnexpectedPortalUse(),
        fallback=object(),
    )

    result = probe_file_selection(services)

    assert result.status is CapabilityStatus.AVAILABLE
    assert result.value == FileSelectionTarget(
        primary_route=FileSelectionRoute.PORTAL,
        fallback_route=FileSelectionRoute.TK,
    )
    assert result.reason_code == "file_selection_portal_route_available"


def test_file_selection_probe_models_legacy_injected_services_as_degraded_route():
    class LegacyService:
        def choose_file(self, **_options):
            return None

    result = probe_file_selection(LegacyService())

    assert result.status is CapabilityStatus.AVAILABLE
    assert result.value == FileSelectionTarget(FileSelectionRoute.INJECTED)
    assert result.reason_code == "file_selection_injected_service_available"
    assert result.source is CapabilitySource.CONSERVATIVE_FALLBACK


def test_file_selection_probe_fails_closed_for_missing_or_invalid_declarations():
    class BrokenService:
        def choose_file(self, **_options):
            return None

        def file_selection_target(self):
            raise RuntimeError("broken route declaration")

    class InvalidService:
        def choose_file(self, **_options):
            return None

        def file_selection_target(self):
            return "portal"

    missing = probe_file_selection(object())
    broken = probe_file_selection(BrokenService())
    invalid = probe_file_selection(InvalidService())

    assert missing.status is CapabilityStatus.UNAVAILABLE
    assert missing.reason_code == "file_selection_service_unavailable"
    assert broken.status is CapabilityStatus.UNKNOWN
    assert broken.reason_code == "file_selection_capability_probe_failed"
    assert invalid.status is CapabilityStatus.UNKNOWN
    assert invalid.reason_code == "file_selection_capability_probe_failed"


def test_file_selection_refuses_a_disabled_preflight_without_opening_chooser():
    class BrokenRouteService:
        def choose_file(self, **_options):
            pytest.fail("a disabled route must not open a file picker")

        def file_selection_target(self):
            raise RuntimeError("broken route declaration")

    services = BrokenRouteService()
    preflight = file_selection_preflight(services)

    with pytest.raises(DesktopServiceError, match="could not be determined"):
        choose_authorized_file(
            preflight,
            services,
            title="Open Guided Dive",
        )


class _RoutedFileService:
    def __init__(self, target: FileSelectionTarget):
        self.target = target
        self.calls: list[dict] = []

    def file_selection_target(self) -> FileSelectionTarget:
        return self.target

    def choose_file(self, **options):
        self.calls.append(options)
        return None


def test_typed_file_preflight_authorizes_only_the_matching_adapter_route():
    target = FileSelectionTarget(
        primary_route=FileSelectionRoute.PORTAL,
        fallback_route=FileSelectionRoute.TK,
    )
    services = _RoutedFileService(target)
    parent = object()

    preflight = file_selection_preflight(services)

    assert authorized_file_selection_target(preflight, services) is target
    assert choose_authorized_file(
        preflight,
        services,
        title="Open Guided Dive",
        initial_dir="/maps/_guided_dives",
        parent=parent,
    ) is None
    assert services.calls == [
        {
            "title": "Open Guided Dive",
            "initial_dir": "/maps/_guided_dives",
            "parent": parent,
        }
    ]


def test_file_selection_rejects_a_route_change_before_opening_chooser():
    portal_target = FileSelectionTarget(
        primary_route=FileSelectionRoute.PORTAL,
        fallback_route=FileSelectionRoute.TK,
    )
    services = _RoutedFileService(portal_target)
    preflight = file_selection_preflight(services)
    services.target = FileSelectionTarget(FileSelectionRoute.TK)

    with pytest.raises(DesktopServiceError, match="availability changed"):
        choose_authorized_file(
            preflight,
            services,
            title="Open Guided Dive",
        )

    assert services.calls == []
