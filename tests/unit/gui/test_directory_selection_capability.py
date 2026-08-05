"""Test directory-selection route facts without contacting desktop services."""

from __future__ import annotations

import pytest

from caveviewer.core.capabilities import (
    CapabilitySource,
    CapabilityStatus,
    DirectorySelectionRoute,
    DirectorySelectionTarget,
)
from caveviewer.gui.platform.desktop_services import TkDesktopServices
from caveviewer.gui.platform.desktop_services import DesktopServiceError
from caveviewer.gui.platform.directory_selection import (
    authorized_directory_selection_target,
    choose_authorized_directory,
    directory_selection_preflight,
)
from caveviewer.gui.platform.portal import LinuxPortalDesktopServices
from caveviewer.gui.platform.probes.desktop import probe_directory_selection


def test_directory_selection_probe_reports_tk_without_creating_a_tk_root():
    result = probe_directory_selection(TkDesktopServices())

    assert result.status is CapabilityStatus.AVAILABLE
    assert result.value == DirectorySelectionTarget(DirectorySelectionRoute.TK)
    assert result.reason_code == "directory_selection_tk_route_available"
    assert result.source is CapabilitySource.DETECTED


def test_directory_selection_probe_declares_portal_then_tk_without_transport_work():
    class UnexpectedPortalUse:
        def __getattr__(self, _name):
            pytest.fail("declaring a picker route must not contact the portal")

    services = LinuxPortalDesktopServices(
        portal=UnexpectedPortalUse(),
        fallback=object(),
    )

    result = probe_directory_selection(services)

    assert result.status is CapabilityStatus.AVAILABLE
    assert result.value == DirectorySelectionTarget(
        primary_route=DirectorySelectionRoute.PORTAL,
        fallback_route=DirectorySelectionRoute.TK,
    )
    assert result.reason_code == "directory_selection_portal_route_available"


def test_directory_selection_probe_models_legacy_injected_services_as_degraded_route():
    class LegacyService:
        def choose_directory(self, **_options):
            return None

    result = probe_directory_selection(LegacyService())

    assert result.status is CapabilityStatus.AVAILABLE
    assert result.value == DirectorySelectionTarget(DirectorySelectionRoute.INJECTED)
    assert result.reason_code == "directory_selection_injected_service_available"
    assert result.source is CapabilitySource.CONSERVATIVE_FALLBACK


def test_directory_selection_probe_fails_closed_for_missing_or_invalid_declarations():
    class BrokenService:
        def choose_directory(self, **_options):
            return None

        def directory_selection_target(self):
            raise RuntimeError("broken route declaration")

    class InvalidService:
        def choose_directory(self, **_options):
            return None

        def directory_selection_target(self):
            return "portal"

    missing = probe_directory_selection(object())
    broken = probe_directory_selection(BrokenService())
    invalid = probe_directory_selection(InvalidService())

    assert missing.status is CapabilityStatus.UNAVAILABLE
    assert missing.reason_code == "directory_selection_service_unavailable"
    assert broken.status is CapabilityStatus.UNKNOWN
    assert broken.reason_code == "directory_selection_capability_probe_failed"
    assert invalid.status is CapabilityStatus.UNKNOWN
    assert invalid.reason_code == "directory_selection_capability_probe_failed"


class _RoutedDirectoryService:
    def __init__(self, target: DirectorySelectionTarget):
        self.target = target
        self.calls: list[dict] = []

    def directory_selection_target(self) -> DirectorySelectionTarget:
        return self.target

    def choose_directory(self, **options):
        self.calls.append(options)
        return None


def test_typed_directory_preflight_authorizes_only_the_matching_adapter_route():
    target = DirectorySelectionTarget(
        primary_route=DirectorySelectionRoute.PORTAL,
        fallback_route=DirectorySelectionRoute.TK,
    )
    services = _RoutedDirectoryService(target)

    preflight = directory_selection_preflight(services)

    assert authorized_directory_selection_target(preflight, services) is target
    assert choose_authorized_directory(
        preflight,
        services,
        title="Open Map Folder",
        initial_dir="/maps",
        parent=object(),
    ) is None
    assert len(services.calls) == 1
    assert services.calls[0]["title"] == "Open Map Folder"
    assert services.calls[0]["initial_dir"] == "/maps"
    assert services.calls[0]["parent"] is not None


def test_directory_selection_rejects_a_route_change_before_opening_chooser():
    portal_target = DirectorySelectionTarget(
        primary_route=DirectorySelectionRoute.PORTAL,
        fallback_route=DirectorySelectionRoute.TK,
    )
    services = _RoutedDirectoryService(portal_target)
    preflight = directory_selection_preflight(services)
    services.target = DirectorySelectionTarget(DirectorySelectionRoute.TK)

    with pytest.raises(DesktopServiceError, match="availability changed"):
        choose_authorized_directory(
            preflight,
            services,
            title="Open Map Folder",
        )

    assert services.calls == []
