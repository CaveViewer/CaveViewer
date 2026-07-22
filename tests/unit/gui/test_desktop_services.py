"""Exercise desktop-service selection, portal states, and Linux fallbacks."""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from types import SimpleNamespace

import pytest

from caveviewer.gui.platform.desktop_services import (
    DesktopServiceError,
    DirectorySelection,
    FileSelection,
    TkDesktopServices,
    get_desktop_services,
    NoopDesktopInhibitor,
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

requires_dbus_fast = pytest.mark.skipif(
    importlib.util.find_spec("dbus_fast") is None,
    reason="dbus-fast is required for low-level Linux D-Bus portal transport tests",
)


class FakeTransport:
    def __init__(
        self,
        *,
        chooser_response=None,
        reveal_response=None,
        file_response=None,
        save_response=None,
        open_response=None,
        error=None,
    ):
        self.chooser_response = chooser_response
        self.reveal_response = reveal_response
        self.file_response = file_response
        self.save_response = save_response
        self.open_response = open_response
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

    def choose_file(self, **options):
        self.calls.append(("choose_file", options))
        if self.error:
            raise self.error
        return self.file_response

    def save_file(self, **options):
        self.calls.append(("save_file", options))
        if self.error:
            raise self.error
        return self.save_response

    def open_uri(self, uri, **options):
        self.calls.append(("open_uri", uri, options))
        if self.error:
            raise self.error
        return self.open_response

    def open_path(self, path, **options):
        self.calls.append(("open_path", path, options))
        if self.error:
            raise self.error
        return self.open_response

    def notify(self, notification_id, title, body, *, priority):
        self.calls.append(("notify", notification_id, title, body, priority))
        if self.error:
            raise self.error

    def withdraw_notification(self, notification_id):
        self.calls.append(("withdraw_notification", notification_id))
        if self.error:
            raise self.error

    def inhibit(self, *, reason, parent_window, flags):
        self.calls.append(("inhibit", reason, parent_window, flags))
        if self.error:
            raise self.error
        return NoopDesktopInhibitor()


class FakeFallback:
    def __init__(self, selection=None, file_selection=None):
        self.selection = selection
        self.file_selection = file_selection
        self.calls = []

    def choose_directory(self, **options):
        self.calls.append(("choose", options))
        return self.selection

    def reveal_path(self, path, *, parent=None):
        self.calls.append(("reveal", path, parent))

    def choose_file(self, **options):
        self.calls.append(("choose_file", options))
        return self.file_selection

    def save_file(self, **options):
        self.calls.append(("save_file", options))
        return self.file_selection

    def open_uri(self, uri, *, parent=None):
        self.calls.append(("open_uri", uri, parent))

    def open_path(self, path, *, parent=None):
        self.calls.append(("open_path", path, parent))

    def notify(self, notification_id, title, body, *, priority):
        self.calls.append(("notify", notification_id, title, body, priority))

    def withdraw_notification(self, notification_id):
        self.calls.append(("withdraw_notification", notification_id))

    def inhibit_idle_suspend(self, reason, *, parent=None):
        self.calls.append(("inhibit", reason, parent))
        return NoopDesktopInhibitor()


def test_directory_selection_decodes_file_uri(tmp_path):
    selected_dir = tmp_path / "Cave Maps"
    selected_dir.mkdir()

    selection = DirectorySelection.from_uri(selected_dir.as_uri())

    assert selection.path == str(selected_dir)
    assert selection.uri == selected_dir.as_uri()


def test_directory_selection_rejects_nonlocal_uri():
    with pytest.raises(DesktopServiceError, match="unsupported"):
        DirectorySelection.from_uri("smb://server/maps/cave")


def test_file_selection_decodes_file_uri(tmp_path):
    selected_file = tmp_path / "Cave Map.glb"
    selected_file.write_bytes(b"glb")

    selection = FileSelection.from_uri(selected_file.as_uri())

    assert selection.path == str(selected_file)
    assert selection.uri == selected_file.as_uri()


def test_portal_state_machine_rejects_invalid_transition():
    machine = PortalRequestStateMachine()

    with pytest.raises(RuntimeError, match="Invalid portal request transition"):
        machine.transition(PortalRequestState.COMPLETED)


def test_portal_state_machine_accepts_terminal_lifecycle_paths():
    completed = PortalRequestStateMachine()
    completed.transition(PortalRequestState.REQUESTING)
    completed.transition(PortalRequestState.WAITING)
    completed.transition(PortalRequestState.COMPLETED)
    assert completed.state is PortalRequestState.COMPLETED

    cancelled = PortalRequestStateMachine()
    cancelled.transition(PortalRequestState.REQUESTING)
    cancelled.transition(PortalRequestState.WAITING)
    cancelled.transition(PortalRequestState.CANCELLED)
    assert cancelled.state is PortalRequestState.CANCELLED

    request_failure = PortalRequestStateMachine()
    request_failure.transition(PortalRequestState.REQUESTING)
    request_failure.transition(PortalRequestState.FAILED)
    assert request_failure.state is PortalRequestState.FAILED

    wait_failure = PortalRequestStateMachine()
    wait_failure.transition(PortalRequestState.REQUESTING)
    wait_failure.transition(PortalRequestState.WAITING)
    wait_failure.transition(PortalRequestState.FAILED)
    assert wait_failure.state is PortalRequestState.FAILED


def test_desktop_service_factory_prefers_portals_on_linux(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")

    services = get_desktop_services()

    assert isinstance(services, LinuxPortalDesktopServices)


def test_desktop_service_factory_uses_tk_fallback_off_linux(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")

    services = get_desktop_services()

    assert isinstance(services, TkDesktopServices)


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


def test_portal_file_chooser_and_save_complete_with_decoded_selection(tmp_path):
    selected_file = tmp_path / "survey.glb"
    selected_file.write_bytes(b"glb")
    transport = FakeTransport(
        file_response=PortalResponse(0, {"uris": [selected_file.as_uri()]}),
        save_response=PortalResponse(0, {"uris": [selected_file.as_uri()]}),
    )
    client = XdgPortalClient(transport)

    opened = client.choose_file(
        title="Open map", initial_dir=str(tmp_path), parent_window="x11:1a"
    )
    saved = client.save_file(
        title="Save map",
        initial_dir=str(tmp_path),
        initial_name="survey.glb",
        parent_window="x11:1a",
    )

    assert opened == FileSelection.from_path(str(selected_file))
    assert saved == FileSelection.from_path(str(selected_file))
    assert transport.calls == [
        (
            "choose_file",
            {
                "title": "Open map",
                "initial_dir": str(tmp_path),
                "parent_window": "x11:1a",
            },
        ),
        (
            "save_file",
            {
                "title": "Save map",
                "initial_dir": str(tmp_path),
                "initial_name": "survey.glb",
                "parent_window": "x11:1a",
            },
        ),
    ]


def test_portal_open_notification_and_inhibit_delegate_to_transport():
    transport = FakeTransport(open_response=PortalResponse(0))
    client = XdgPortalClient(transport)

    client.open_uri("https://example.invalid", parent_window="")
    client.open_path("/maps/survey.glb", parent_window="")
    client.notify("download", "Ready", "Map downloaded", priority="normal")
    client.withdraw_notification("download")
    inhibitor = client.inhibit_idle_suspend(reason="Importing map", parent_window="")
    inhibitor.close()

    assert transport.calls == [
        ("open_uri", "https://example.invalid", {"parent_window": ""}),
        ("open_path", "/maps/survey.glb", {"parent_window": ""}),
        ("notify", "download", "Ready", "Map downloaded", "normal"),
        ("withdraw_notification", "download"),
        ("inhibit", "Importing map", "", 12),
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


def test_linux_file_chooser_failure_uses_tk_fallback():
    transport = FakeTransport(error=DesktopServiceError("session bus missing"))
    fallback_selection = FileSelection.from_path("/fallback.glb")
    fallback = FakeFallback(file_selection=fallback_selection)
    services = LinuxPortalDesktopServices(
        portal=XdgPortalClient(transport), fallback=fallback
    )

    result = services.choose_file(
        title="Open map", initial_dir="/maps", parent="owner"
    )

    assert result == fallback_selection
    assert fallback.calls == [
        (
            "choose_file",
            {"title": "Open map", "initial_dir": "/maps", "parent": "owner"},
        )
    ]


def test_linux_open_uri_failure_uses_fallback_for_remote_uri():
    transport = FakeTransport(error=DesktopServiceError("session bus missing"))
    fallback = FakeFallback()
    services = LinuxPortalDesktopServices(
        portal=XdgPortalClient(transport), fallback=fallback
    )

    services.open_uri("https://example.invalid/map", parent="owner")

    assert fallback.calls == [
        ("open_uri", "https://example.invalid/map", "owner")
    ]


def test_linux_notify_withdraw_and_inhibit_failures_use_fallback():
    transport = FakeTransport(error=DesktopServiceError("session bus missing"))
    fallback = FakeFallback()
    services = LinuxPortalDesktopServices(
        portal=XdgPortalClient(transport), fallback=fallback
    )

    services.notify("sample", "Ready", "Downloaded", priority="normal")
    services.withdraw_notification("sample")
    inhibitor = services.inhibit_idle_suspend("Downloading map", parent="owner")
    inhibitor.close()

    assert fallback.calls == [
        ("notify", "sample", "Ready", "Downloaded", "normal"),
        ("withdraw_notification", "sample"),
        ("inhibit", "Downloading map", "owner"),
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
        self.call_request = None
        self.direct_calls = []
        self.error = error

    async def _perform_request(self, **request):
        self.request = request
        if self.error:
            raise self.error
        return PortalResponse(0)

    async def _perform_call(self, **request):
        self.call_request = request
        if self.error:
            raise self.error

    async def _call(self, bus, **request):
        del bus
        self.direct_calls.append(request)
        if self.error:
            raise self.error
        return SimpleNamespace(body=["/org/freedesktop/portal/desktop/request/test"])


@requires_dbus_fast
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


@requires_dbus_fast
def test_dbus_file_chooser_and_save_request_options(tmp_path):
    transport = CapturingDbusTransport()

    asyncio.run(
        transport._choose_file(
            title="Open map",
            initial_dir=str(tmp_path),
            parent_window="x11:2a",
        )
    )

    assert transport.request["interface"].endswith("FileChooser")
    assert transport.request["member"] == "OpenFile"
    assert transport.request["minimum_version"] == 1
    parent, title, options = transport.request["body"]
    assert (parent, title) == ("x11:2a", "Open map")
    assert "directory" not in options
    assert options["modal"].value is True
    assert options["current_folder"].value == bytes(tmp_path) + b"\0"

    asyncio.run(
        transport._save_file(
            title="Save map",
            initial_dir=str(tmp_path),
            initial_name="survey.glb",
            parent_window="x11:2a",
        )
    )

    assert transport.request["member"] == "SaveFile"
    parent, title, options = transport.request["body"]
    assert (parent, title) == ("x11:2a", "Save map")
    assert options["current_folder"].value == bytes(tmp_path) + b"\0"
    assert options["current_name"].value == "survey.glb"


@requires_dbus_fast
def test_dbus_open_uri_request_uses_openuri_portal():
    transport = CapturingDbusTransport()

    asyncio.run(
        transport._open_uri("https://example.invalid", parent_window="x11:2a")
    )

    assert transport.request["interface"].endswith("OpenURI")
    assert transport.request["member"] == "OpenURI"
    assert transport.request["minimum_version"] == 1
    assert transport.request["body"][0:2] == ["x11:2a", "https://example.invalid"]


@requires_dbus_fast
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


@requires_dbus_fast
def test_dbus_open_file_closes_descriptor_after_success_and_failure(tmp_path):
    payload = tmp_path / "survey.glb"
    payload.write_bytes(b"glb")
    closed = []
    transport_options = dict(
        open_file=lambda *_args, **_kwargs: 73,
        close_file=lambda descriptor: closed.append(descriptor),
    )
    transport = CapturingDbusTransport(**transport_options)

    asyncio.run(transport._open_path(str(payload), parent_window=""))

    assert transport.request["member"] == "OpenFile"
    assert transport.request["minimum_version"] == 2
    assert transport.request["unix_fds"] == [73]
    assert transport.request["body"][1] == 0
    assert closed == [73]

    failed = CapturingDbusTransport(
        error=RuntimeError("portal failed"), **transport_options
    )
    with pytest.raises(RuntimeError, match="portal failed"):
        asyncio.run(failed._open_path(str(payload), parent_window=""))
    assert closed == [73, 73]


@requires_dbus_fast
def test_dbus_notification_calls_portal_methods():
    transport = CapturingDbusTransport()

    asyncio.run(
        transport._notify(
            "map-library-download",
            "Download ready",
            "Open the map library entry.",
            priority="normal",
        )
    )

    assert transport.call_request["interface"].endswith("Notification")
    assert transport.call_request["member"] == "AddNotification"
    notification_id, payload = transport.call_request["body"]
    assert notification_id == "map-library-download"
    assert payload["title"].value == "Download ready"
    assert payload["body"].value == "Open the map library entry."
    assert payload["priority"].value == "normal"

    asyncio.run(transport._withdraw_notification("map-library-download"))

    assert transport.call_request["member"] == "RemoveNotification"
    assert transport.call_request["body"] == ["map-library-download"]


@requires_dbus_fast
def test_dbus_inhibit_request_uses_idle_and_suspend_flags():
    transport = CapturingDbusTransport()

    request_path = asyncio.run(
        transport._start_inhibit(
            object(),
            reason="Importing map",
            parent_window="x11:2a",
            flags=12,
        )
    )

    assert request_path.endswith("/request/test")
    request = transport.direct_calls[0]
    assert request["interface"].endswith("Inhibit")
    assert request["member"] == "Inhibit"
    parent, flags, options = request["body"]
    assert (parent, flags) == ("x11:2a", 12)
    assert options["reason"].value == "Importing map"


@requires_dbus_fast
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
