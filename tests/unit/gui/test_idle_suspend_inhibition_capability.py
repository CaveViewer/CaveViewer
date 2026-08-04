"""Test optional idle/suspend inhibition routes without acquiring real handles."""

from __future__ import annotations

import pytest

from caveviewer.core.capabilities import (
    CapabilitySource,
    CapabilityStatus,
    IdleSuspendInhibitionRoute,
    IdleSuspendInhibitionTarget,
)
from caveviewer.gui.platform.desktop_inhibition import (
    acquire_authorized_idle_suspend_inhibitor,
    acquire_idle_suspend_inhibitor,
    authorized_idle_suspend_inhibition_target,
    idle_suspend_inhibition_preflight,
    release_desktop_inhibitor,
)
from caveviewer.gui.platform.desktop_services import TkDesktopServices
from caveviewer.gui.platform.portal import LinuxPortalDesktopServices
from caveviewer.gui.platform.probes.desktop import probe_idle_suspend_inhibition


def test_inhibition_probe_reports_portal_then_noop_without_transport_work():
    class UnexpectedPortalUse:
        def __getattr__(self, _name):
            pytest.fail("declaring an inhibition route must not contact the portal")

    services = LinuxPortalDesktopServices(
        portal=UnexpectedPortalUse(),
        fallback=object(),
    )

    result = probe_idle_suspend_inhibition(services)

    assert result.status is CapabilityStatus.AVAILABLE
    assert result.value == IdleSuspendInhibitionTarget(
        primary_route=IdleSuspendInhibitionRoute.PORTAL,
        fallback_route=IdleSuspendInhibitionRoute.NOOP,
    )
    assert result.reason_code == "idle_suspend_inhibition_portal_route_available"


def test_inhibition_probe_treats_portable_noop_as_unavailable_without_tk_work():
    result = probe_idle_suspend_inhibition(TkDesktopServices())

    assert result.status is CapabilityStatus.UNAVAILABLE
    assert result.value is None
    assert result.reason_code == "idle_suspend_inhibition_service_unavailable"
    assert result.source is CapabilitySource.DETECTED
    assert dict(result.evidence) == {
        "primary_route": "noop",
        "fallback_route": None,
    }


def test_inhibition_probe_models_legacy_injected_services_as_degraded_route():
    class LegacyService:
        def inhibit_idle_suspend(self, *_args, **_kwargs):
            return object()

    result = probe_idle_suspend_inhibition(LegacyService())

    assert result.status is CapabilityStatus.AVAILABLE
    assert result.value == IdleSuspendInhibitionTarget(
        IdleSuspendInhibitionRoute.INJECTED
    )
    assert result.reason_code == "idle_suspend_inhibition_injected_service_available"
    assert result.source is CapabilitySource.CONSERVATIVE_FALLBACK


def test_inhibition_probe_fails_closed_for_missing_or_invalid_declarations():
    class BrokenService:
        def inhibit_idle_suspend(self, *_args, **_kwargs):
            return object()

        def idle_suspend_inhibition_target(self):
            raise RuntimeError("broken route declaration")

    class InvalidService:
        def inhibit_idle_suspend(self, *_args, **_kwargs):
            return object()

        def idle_suspend_inhibition_target(self):
            return "portal"

    missing = probe_idle_suspend_inhibition(object())
    broken = probe_idle_suspend_inhibition(BrokenService())
    invalid = probe_idle_suspend_inhibition(InvalidService())

    assert missing.status is CapabilityStatus.UNAVAILABLE
    assert missing.reason_code == "idle_suspend_inhibition_service_unavailable"
    assert broken.status is CapabilityStatus.UNKNOWN
    assert broken.reason_code == "idle_suspend_inhibition_capability_probe_failed"
    assert invalid.status is CapabilityStatus.UNKNOWN
    assert invalid.reason_code == "idle_suspend_inhibition_capability_probe_failed"


class _FakeInhibitor:
    def __init__(self, calls: list[tuple]) -> None:
        self.calls = calls

    def close(self) -> None:
        self.calls.append(("close",))


class _RoutedInhibitionService:
    def __init__(
        self,
        target: IdleSuspendInhibitionTarget,
        *,
        fail_acquire: bool = False,
    ) -> None:
        self.target = target
        self.fail_acquire = fail_acquire
        self.calls: list[tuple] = []
        self.inhibitor = _FakeInhibitor(self.calls)

    def idle_suspend_inhibition_target(self) -> IdleSuspendInhibitionTarget:
        return self.target

    def inhibit_idle_suspend(self, reason, *, parent=None):
        self.calls.append(("acquire", reason, parent))
        if self.fail_acquire:
            raise RuntimeError("inhibit transport failed")
        return self.inhibitor


def test_typed_inhibition_preflight_authorizes_matching_adapter_route():
    target = IdleSuspendInhibitionTarget(
        primary_route=IdleSuspendInhibitionRoute.PORTAL,
        fallback_route=IdleSuspendInhibitionRoute.NOOP,
    )
    services = _RoutedInhibitionService(target)
    preflight = idle_suspend_inhibition_preflight(services)
    parent = object()

    assert authorized_idle_suspend_inhibition_target(preflight, services) is target
    assert (
        acquire_authorized_idle_suspend_inhibitor(
            preflight,
            services,
            "Importing Crystal Cave",
            parent=parent,
        )
        is services.inhibitor
    )
    release_desktop_inhibitor(services.inhibitor)
    assert services.calls == [
        ("acquire", "Importing Crystal Cave", parent),
        ("close",),
    ]


def test_inhibition_route_change_or_disabled_route_is_a_best_effort_noop():
    portal_target = IdleSuspendInhibitionTarget(
        primary_route=IdleSuspendInhibitionRoute.PORTAL,
        fallback_route=IdleSuspendInhibitionRoute.NOOP,
    )
    services = _RoutedInhibitionService(portal_target)
    preflight = idle_suspend_inhibition_preflight(services)
    services.target = IdleSuspendInhibitionTarget(IdleSuspendInhibitionRoute.INJECTED)

    assert (
        acquire_authorized_idle_suspend_inhibitor(
            preflight,
            services,
            "Importing Crystal Cave",
        )
        is None
    )
    assert services.calls == []

    class NoopRouteService:
        def idle_suspend_inhibition_target(self):
            return IdleSuspendInhibitionTarget(IdleSuspendInhibitionRoute.NOOP)

        def inhibit_idle_suspend(self, *_args, **_kwargs):
            pytest.fail("an unavailable route must not acquire an inhibitor")

    assert (
        acquire_idle_suspend_inhibitor(
            NoopRouteService(),
            "Importing Crystal Cave",
        )
        is None
    )


def test_inhibition_action_failure_is_a_best_effort_noop():
    target = IdleSuspendInhibitionTarget(IdleSuspendInhibitionRoute.INJECTED)
    services = _RoutedInhibitionService(target, fail_acquire=True)

    assert (
        acquire_idle_suspend_inhibitor(
            services,
            "Importing Crystal Cave",
        )
        is None
    )
    assert services.calls == [("acquire", "Importing Crystal Cave", None)]
