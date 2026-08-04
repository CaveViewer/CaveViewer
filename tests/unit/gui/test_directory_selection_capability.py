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
