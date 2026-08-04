"""Test optional desktop-notification routes without sending real messages."""

from __future__ import annotations

import pytest

from caveviewer.core.capabilities import (
    CapabilitySource,
    CapabilityStatus,
    DesktopNotificationRoute,
    DesktopNotificationTarget,
)
from caveviewer.gui.platform.desktop_notifications import (
    authorized_desktop_notification_target,
    desktop_notification_preflight,
    send_authorized_desktop_notification,
    send_desktop_notification,
    withdraw_authorized_desktop_notification,
)
from caveviewer.gui.platform.desktop_services import TkDesktopServices
from caveviewer.gui.platform.portal import LinuxPortalDesktopServices
from caveviewer.gui.platform.probes.desktop import probe_desktop_notification


def test_notification_probe_reports_portal_then_noop_without_transport_work():
    class UnexpectedPortalUse:
        def __getattr__(self, _name):
            pytest.fail("declaring a notification route must not contact the portal")

    services = LinuxPortalDesktopServices(
        portal=UnexpectedPortalUse(),
        fallback=object(),
    )

    result = probe_desktop_notification(services)

    assert result.status is CapabilityStatus.AVAILABLE
    assert result.value == DesktopNotificationTarget(
        primary_route=DesktopNotificationRoute.PORTAL,
        fallback_route=DesktopNotificationRoute.NOOP,
    )
    assert result.reason_code == "desktop_notification_portal_route_available"


def test_notification_probe_treats_portable_noop_as_unavailable_without_tk_work():
    result = probe_desktop_notification(TkDesktopServices())

    assert result.status is CapabilityStatus.UNAVAILABLE
    assert result.value is None
    assert result.reason_code == "desktop_notification_service_unavailable"
    assert result.source is CapabilitySource.DETECTED
    assert dict(result.evidence) == {
        "primary_route": "noop",
        "fallback_route": None,
    }


def test_notification_probe_models_legacy_injected_services_as_degraded_route():
    class LegacyService:
        def notify(self, *_args, **_kwargs):
            return None

    result = probe_desktop_notification(LegacyService())

    assert result.status is CapabilityStatus.AVAILABLE
    assert result.value == DesktopNotificationTarget(
        DesktopNotificationRoute.INJECTED
    )
    assert result.reason_code == "desktop_notification_injected_service_available"
    assert result.source is CapabilitySource.CONSERVATIVE_FALLBACK


def test_notification_probe_fails_closed_for_missing_or_invalid_declarations():
    class BrokenService:
        def notify(self, *_args, **_kwargs):
            return None

        def desktop_notification_target(self):
            raise RuntimeError("broken route declaration")

    class InvalidService:
        def notify(self, *_args, **_kwargs):
            return None

        def desktop_notification_target(self):
            return "portal"

    missing = probe_desktop_notification(object())
    broken = probe_desktop_notification(BrokenService())
    invalid = probe_desktop_notification(InvalidService())

    assert missing.status is CapabilityStatus.UNAVAILABLE
    assert missing.reason_code == "desktop_notification_service_unavailable"
    assert broken.status is CapabilityStatus.UNKNOWN
    assert broken.reason_code == "desktop_notification_capability_probe_failed"
    assert invalid.status is CapabilityStatus.UNKNOWN
    assert invalid.reason_code == "desktop_notification_capability_probe_failed"


class _RoutedNotificationService:
    def __init__(
        self,
        target: DesktopNotificationTarget,
        *,
        fail_notify: bool = False,
    ) -> None:
        self.target = target
        self.fail_notify = fail_notify
        self.calls: list[tuple] = []

    def desktop_notification_target(self) -> DesktopNotificationTarget:
        return self.target

    def notify(self, notification_id, title, body="", *, priority="normal"):
        self.calls.append(("notify", notification_id, title, body, priority))
        if self.fail_notify:
            raise RuntimeError("notification transport failed")

    def withdraw_notification(self, notification_id):
        self.calls.append(("withdraw", notification_id))


def test_typed_notification_preflight_authorizes_matching_adapter_route():
    target = DesktopNotificationTarget(
        primary_route=DesktopNotificationRoute.PORTAL,
        fallback_route=DesktopNotificationRoute.NOOP,
    )
    services = _RoutedNotificationService(target)
    preflight = desktop_notification_preflight(services)

    assert authorized_desktop_notification_target(preflight, services) is target
    assert send_authorized_desktop_notification(
        preflight,
        services,
        "caveviewer.test",
        "Title",
        "Body",
        priority="high",
    )
    assert withdraw_authorized_desktop_notification(
        preflight,
        services,
        "caveviewer.test",
    )
    assert services.calls == [
        ("notify", "caveviewer.test", "Title", "Body", "high"),
        ("withdraw", "caveviewer.test"),
    ]


def test_notification_route_change_or_disabled_route_is_a_best_effort_noop():
    portal_target = DesktopNotificationTarget(
        primary_route=DesktopNotificationRoute.PORTAL,
        fallback_route=DesktopNotificationRoute.NOOP,
    )
    services = _RoutedNotificationService(portal_target)
    preflight = desktop_notification_preflight(services)
    services.target = DesktopNotificationTarget(DesktopNotificationRoute.INJECTED)

    assert not send_authorized_desktop_notification(
        preflight,
        services,
        "caveviewer.test",
        "Title",
    )
    assert services.calls == []

    class NoopRouteService:
        def desktop_notification_target(self):
            return DesktopNotificationTarget(DesktopNotificationRoute.NOOP)

        def notify(self, *_args, **_kwargs):
            pytest.fail("an unavailable route must not send a notification")

    assert not send_desktop_notification(
        NoopRouteService(),
        "caveviewer.test",
        "Title",
    )


def test_notification_action_failure_remains_a_best_effort_noop():
    target = DesktopNotificationTarget(DesktopNotificationRoute.INJECTED)
    services = _RoutedNotificationService(target, fail_notify=True)

    assert not send_desktop_notification(
        services,
        "caveviewer.test",
        "Title",
    )
    assert services.calls == [
        ("notify", "caveviewer.test", "Title", "", "normal"),
    ]
