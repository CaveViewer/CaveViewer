"""Test static capability routes for focused non-executing package reveal."""

from __future__ import annotations

import pytest

from caveviewer.core.capabilities import CapabilityStatus, UpdatePackageRevealRoute
from caveviewer.gui.platform.probes.update_package_reveal import (
    probe_update_package_reveal,
)
from caveviewer.gui.platform.update_package_reveal import (
    create_update_package_reveal_adapter,
)


class FakeDesktopServices:
    def __init__(self) -> None:
        self.revealed_paths = []

    def reveal_path(self, path, *, parent=None) -> None:
        self.revealed_paths.append((path, parent))


@pytest.mark.parametrize(
    ("platform_name", "expected_route", "expected_label"),
    [
        ("darwin", UpdatePackageRevealRoute.FINDER, "Show in Finder"),
        ("win32", UpdatePackageRevealRoute.EXPLORER, "Show in Explorer"),
        (
            "linux",
            UpdatePackageRevealRoute.DESKTOP_SERVICE,
            "Open Download Folder",
        ),
    ],
)
def test_composed_reveal_adapter_declares_static_route_without_action(
    platform_name,
    expected_route,
    expected_label,
):
    desktop_services = FakeDesktopServices()
    reveal_adapter = create_update_package_reveal_adapter(
        platform_name=platform_name,
        desktop_services=desktop_services,
    )

    capability = probe_update_package_reveal(reveal_adapter)

    assert capability.status is CapabilityStatus.AVAILABLE
    assert capability.value is expected_route
    assert capability.evidence == {"route": expected_route.value}
    assert reveal_adapter.reveal_action_label() == expected_label
    assert desktop_services.revealed_paths == []


def test_unknown_platform_has_no_update_package_reveal_route():
    capability = probe_update_package_reveal(
        create_update_package_reveal_adapter(platform_name="freebsd")
    )

    assert capability.status is CapabilityStatus.UNAVAILABLE
    assert capability.reason_code == "update_package_reveal_route_unsupported"
    assert capability.evidence == {"route": "unsupported"}


def test_probe_fails_closed_for_missing_or_invalid_route_declarations():
    class InvalidRouteAdapter:
        def reveal_route(self):
            return "finder"

    unavailable = probe_update_package_reveal(object())
    unknown = probe_update_package_reveal(InvalidRouteAdapter())

    assert unavailable.status is CapabilityStatus.UNAVAILABLE
    assert unavailable.reason_code == "update_package_reveal_adapter_unavailable"
    assert unknown.status is CapabilityStatus.UNKNOWN
    assert unknown.reason_code == "update_package_reveal_capability_probe_failed"
