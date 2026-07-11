"""Exercise desktop-service selection, portal states, and Linux fallbacks."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from caveviewer.gui.platform.desktop_services import (
    DesktopServiceError,
    DirectorySelection,
)
from caveviewer.gui.platform.portal import (
    LinuxPortalDesktopServices,
    PortalRequestError,
    PortalRequestState,
    PortalRequestStateMachine,
    PortalResponse,
    DbusPortalTransport,
    _PortalResponseWaiter,
    XdgPortalClient,
    portal_parent_window,
)


class FakeTransport:
    def __init__(self, *, chooser_response=None, reveal_response=None, error=None):
        self.chooser_response = chooser_response
        self.reveal_response = reveal_response
        self.error = error
        self.calls = []

    def choose_directory(self, **options):
        self.calls.append(("choose", options))
        if self.error:
            raise self.error
        return self.chooser_response

    def reveal_path(self, path, **options):
        self.calls.append(("reveal", path, options))
        if self.error:
            raise self.error
        return self.reveal_response


class FakeFallback:
    def __init__(self, selection=None):
        self.selection = selection
        self.calls = []

    def choose_directory(self, **options):
        self.calls.append(("choose", options))
        return self.selection

    def reveal_path(self, path, *, parent=None):
        self.calls.append(("reveal", path, parent))


def test_directory_selection_decodes_file_uri(tmp_path):
    selected_dir = tmp_path / "Cave Maps"
    selected_dir.mkdir()

    selection = DirectorySelection.from_uri(selected_dir.as_uri())

    assert selection.path == str(selected_dir)
    assert selection.uri == selected_dir.as_uri()


def test_directory_selection_rejects_nonlocal_uri():
    with pytest.raises(DesktopServiceError, match="unsupported"):
        DirectorySelection.from_uri("smb://server/maps/cave")


def test_portal_state_machine_rejects_invalid_transition():
    machine = PortalRequestStateMachine()

    with pytest.raises(RuntimeError, match="Invalid portal request transition"):
        machine.transition(PortalRequestState.COMPLETED)


def test_portal_chooser_completes_with_decoded_selection(tmp_path):
    selected_dir = tmp_path / "Cave Maps"
    selected_dir.mkdir()
    transport = FakeTransport(
        chooser_response=PortalResponse(0, {"uris": [selected_dir.as_uri()]})
    )
    client = XdgPortalClient(transport)

    selection = client.choose_directory(
        title="Select map", initial_dir=str(tmp_path), parent_window="x11:1a"
    )

    assert selection == DirectorySelection.from_path(str(selected_dir))
    assert client.last_state is PortalRequestState.COMPLETED
    assert transport.calls == [
        (
            "choose",
            {
                "title": "Select map",
                "initial_dir": str(tmp_path),
                "parent_window": "x11:1a",
            },
        )
    ]


def test_portal_cancellation_does_not_use_fallback():
    portal = XdgPortalClient(FakeTransport(chooser_response=PortalResponse(1)))
    fallback = FakeFallback(DirectorySelection.from_path("/fallback"))
    services = LinuxPortalDesktopServices(portal=portal, fallback=fallback)

    assert services.choose_directory(title="Select map") is None
    assert portal.last_state is PortalRequestState.CANCELLED
    assert fallback.calls == []


def test_portal_failure_uses_tk_fallback():
    transport = FakeTransport(error=DesktopServiceError("session bus missing"))
    fallback_selection = DirectorySelection.from_path("/fallback")
    fallback = FakeFallback(fallback_selection)
    services = LinuxPortalDesktopServices(
        portal=XdgPortalClient(transport), fallback=fallback
    )

    result = services.choose_directory(
        title="Select map", initial_dir="/maps", parent="owner"
    )

    assert result == fallback_selection
    assert fallback.calls == [
        (
            "choose",
            {"title": "Select map", "initial_dir": "/maps", "parent": "owner"},
        )
    ]


def test_portal_nonzero_failure_is_explicit():
    client = XdgPortalClient(FakeTransport(chooser_response=PortalResponse(2)))

    with pytest.raises(PortalRequestError, match="failed"):
        client.choose_directory(
            title="Select map", initial_dir=None, parent_window=""
        )

    assert client.last_state is PortalRequestState.FAILED


def test_linux_reveal_falls_back_to_containing_directory(tmp_path, monkeypatch):
    payload = tmp_path / "CaveViewer.AppImage"
    payload.write_bytes(b"package")
    launched = []
    transport = FakeTransport(error=DesktopServiceError("portal unavailable"))
    services = LinuxPortalDesktopServices(
        portal=XdgPortalClient(transport), fallback=FakeFallback()
    )
    monkeypatch.setattr(
        "caveviewer.gui.platform.portal.subprocess.Popen",
        lambda command: launched.append(command),
    )

    services.reveal_path(str(payload))

    assert launched == [["xdg-open", str(tmp_path)]]


def test_x11_parent_handle_and_wayland_safe_fallback(monkeypatch):
    class Parent:
        def winfo_id(self):
            return 42

    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.setenv("XDG_SESSION_TYPE", "x11")
    assert portal_parent_window(Parent()) == "x11:2a"

    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    assert portal_parent_window(Parent()) == ""


class CapturingDbusTransport(DbusPortalTransport):
    def __init__(self, error=None, **transport_options):
        super().__init__(**transport_options)
        self.request = None
        self.error = error

    async def _perform_request(self, **request):
        self.request = request
        if self.error:
            raise self.error
        return PortalResponse(0)


def test_dbus_directory_request_sets_versioned_directory_options(tmp_path):
    transport = CapturingDbusTransport()

    asyncio.run(
        transport._choose_directory(
            title="Select map",
            initial_dir=str(tmp_path),
            parent_window="x11:2a",
        )
    )

    assert transport.request["interface"].endswith("FileChooser")
    assert transport.request["member"] == "OpenFile"
    assert transport.request["minimum_version"] == 3
    parent, title, options = transport.request["body"]
    assert (parent, title) == ("x11:2a", "Select map")
    assert options["directory"].value is True
    assert options["modal"].value is True
    assert options["current_folder"].value == bytes(tmp_path) + b"\0"


def test_dbus_reveal_closes_descriptor_after_success_and_failure(tmp_path):
    payload = tmp_path / "CaveViewer.AppImage"
    payload.write_bytes(b"package")
    closed = []
    transport_options = dict(
        open_file=lambda *_args, **_kwargs: 71,
        close_file=lambda descriptor: closed.append(descriptor),
    )
    transport = CapturingDbusTransport(**transport_options)

    asyncio.run(transport._reveal_path(str(payload), parent_window=""))

    assert transport.request["member"] == "OpenDirectory"
    assert transport.request["unix_fds"] == [71]
    assert transport.request["body"][1] == 0
    assert closed == [71]

    failed = CapturingDbusTransport(
        error=RuntimeError("portal failed"), **transport_options
    )
    with pytest.raises(RuntimeError, match="portal failed"):
        asyncio.run(failed._reveal_path(str(payload), parent_window=""))
    assert closed == [71, 71]


def test_response_waiter_keeps_signal_that_arrives_before_handle_return():
    from dbus_fast.constants import MessageType

    waiter = _PortalResponseWaiter()
    message = SimpleNamespace(
        message_type=MessageType.SIGNAL,
        interface="org.freedesktop.portal.Request",
        member="Response",
        path="/request/caveviewer",
        body=[0, {"uris": SimpleNamespace(value=["file:///maps/cave"]) }],
    )

    assert waiter.handle_message(message)
    response = asyncio.run(waiter.wait(message.path))

    assert response == PortalResponse(0, {"uris": ["file:///maps/cave"]})
