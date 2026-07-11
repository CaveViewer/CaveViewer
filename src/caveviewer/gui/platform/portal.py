"""XDG Desktop Portal file selection and reveal integration for Linux."""

from __future__ import annotations

import asyncio
import enum
import os
import secrets
import subprocess
from dataclasses import dataclass, field
from typing import Any, Protocol

from caveviewer.core.logging_utils import get_logger
from caveviewer.gui.platform.desktop_services import (
    DesktopServiceError,
    DesktopServices,
    DirectorySelection,
)


_LOG = get_logger("CaveViewer")
_PORTAL_DESTINATION = "org.freedesktop.portal.Desktop"
_PORTAL_PATH = "/org/freedesktop/portal/desktop"
_REQUEST_INTERFACE = "org.freedesktop.portal.Request"
_FILE_CHOOSER_INTERFACE = "org.freedesktop.portal.FileChooser"
_OPEN_URI_INTERFACE = "org.freedesktop.portal.OpenURI"
_RESPONSE_MATCH_RULE = (
    "type='signal',interface='org.freedesktop.portal.Request',member='Response'"
)


class PortalUnavailableError(DesktopServiceError):
    """The session has no compatible desktop portal."""


class PortalRequestError(DesktopServiceError):
    """A portal accepted a request but could not complete it."""


class PortalRequestState(enum.Enum):
    """Lifecycle shared by chooser and reveal portal requests."""

    IDLE = "idle"
    REQUESTING = "requesting"
    WAITING = "waiting"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"


_ALLOWED_TRANSITIONS = {
    PortalRequestState.IDLE: {PortalRequestState.REQUESTING},
    PortalRequestState.REQUESTING: {
        PortalRequestState.WAITING,
        PortalRequestState.FAILED,
    },
    PortalRequestState.WAITING: {
        PortalRequestState.COMPLETED,
        PortalRequestState.CANCELLED,
        PortalRequestState.FAILED,
    },
}


@dataclass
class PortalRequestStateMachine:
    """Validate request transitions independently from D-Bus side effects."""

    state: PortalRequestState = PortalRequestState.IDLE

    def transition(self, next_state: PortalRequestState) -> None:
        if next_state not in _ALLOWED_TRANSITIONS.get(self.state, set()):
            raise RuntimeError(
                f"Invalid portal request transition: {self.state.value} -> "
                f"{next_state.value}"
            )
        self.state = next_state


@dataclass(frozen=True)
class PortalResponse:
    """Unpacked response emitted by ``org.freedesktop.portal.Request``."""

    code: int
    results: dict[str, Any] = field(default_factory=dict)


class PortalTransport(Protocol):
    """Blocking transport boundary used by the platform service."""

    def choose_directory(
        self, *, title: str, initial_dir: str | None, parent_window: str
    ) -> PortalResponse:
        ...

    def reveal_path(self, path: str, *, parent_window: str) -> PortalResponse:
        ...


class XdgPortalClient:
    """Translate typed desktop requests into portal response states."""

    def __init__(self, transport: PortalTransport | None = None) -> None:
        self._transport = transport or DbusPortalTransport()
        self.last_state = PortalRequestState.IDLE

    def choose_directory(
        self, *, title: str, initial_dir: str | None, parent_window: str
    ) -> DirectorySelection | None:
        machine = PortalRequestStateMachine()
        machine.transition(PortalRequestState.REQUESTING)
        try:
            machine.transition(PortalRequestState.WAITING)
            response = self._transport.choose_directory(
                title=title,
                initial_dir=initial_dir,
                parent_window=parent_window,
            )
        except Exception:
            machine.transition(PortalRequestState.FAILED)
            self.last_state = machine.state
            raise

        if response.code == 1:
            machine.transition(PortalRequestState.CANCELLED)
            self.last_state = machine.state
            return None
        if response.code != 0:
            machine.transition(PortalRequestState.FAILED)
            self.last_state = machine.state
            raise PortalRequestError(
                f"The desktop portal failed the directory request ({response.code})."
            )

        uris = response.results.get("uris") or ()
        if not uris:
            machine.transition(PortalRequestState.FAILED)
            self.last_state = machine.state
            raise PortalRequestError(
                "The desktop portal completed without returning a directory."
            )
        selection = DirectorySelection.from_uri(str(uris[0]))
        machine.transition(PortalRequestState.COMPLETED)
        self.last_state = machine.state
        return selection

    def reveal_path(self, path: str, *, parent_window: str) -> None:
        machine = PortalRequestStateMachine()
        machine.transition(PortalRequestState.REQUESTING)
        try:
            machine.transition(PortalRequestState.WAITING)
            response = self._transport.reveal_path(
                path, parent_window=parent_window
            )
        except Exception:
            machine.transition(PortalRequestState.FAILED)
            self.last_state = machine.state
            raise
        if response.code == 1:
            machine.transition(PortalRequestState.CANCELLED)
            self.last_state = machine.state
            return
        if response.code != 0:
            machine.transition(PortalRequestState.FAILED)
            self.last_state = machine.state
            raise PortalRequestError(
                f"The desktop portal failed the reveal request ({response.code})."
            )
        machine.transition(PortalRequestState.COMPLETED)
        self.last_state = machine.state


class LinuxPortalDesktopServices:
    """Portal-first Linux implementation with conservative host fallbacks."""

    def __init__(
        self,
        *,
        portal: XdgPortalClient | None = None,
        fallback: DesktopServices,
    ) -> None:
        self._portal = portal or XdgPortalClient()
        self._fallback = fallback

    def choose_directory(
        self,
        *,
        title: str,
        initial_dir: str | None = None,
        parent: Any | None = None,
    ) -> DirectorySelection | None:
        try:
            return self._portal.choose_directory(
                title=title,
                initial_dir=initial_dir,
                parent_window=portal_parent_window(parent),
            )
        except Exception as exc:
            # This is the platform boundary: malformed replies and transport
            # library failures are just as recoverable as a missing portal.
            _LOG.warning(f"Desktop portal directory chooser unavailable: {exc}")
            return self._fallback.choose_directory(
                title=title, initial_dir=initial_dir, parent=parent
            )

    def reveal_path(self, path: str, *, parent: Any | None = None) -> None:
        try:
            self._portal.reveal_path(
                os.path.abspath(path),
                parent_window=portal_parent_window(parent),
            )
            return
        except Exception as exc:
            _LOG.warning(f"Desktop portal file reveal unavailable: {exc}")

        # xdg-open is intentionally a fallback only.  Opening the containing
        # directory never executes the downloaded package itself.
        containing_dir = os.path.dirname(os.path.abspath(path)) or os.path.expanduser("~")
        subprocess.Popen(["xdg-open", containing_dir])


def portal_parent_window(parent: Any | None) -> str:
    """Return a portal parent identifier when the toolkit can provide one."""
    if parent is None:
        return ""
    exported = getattr(parent, "portal_parent_window", None)
    if callable(exported):
        exported = exported()
    if exported:
        return str(exported)
    # Tk exposes an XID but not the xdg-foreign handle portals require for a
    # native Wayland parent.  An empty handle is valid and safer on Wayland.
    if os.environ.get("WAYLAND_DISPLAY") or os.environ.get("XDG_SESSION_TYPE") == "wayland":
        return ""
    try:
        return f"x11:{int(parent.winfo_id()):x}"
    except (AttributeError, TypeError, ValueError):
        return ""


class _PortalResponseWaiter:
    """Collect responses even if a fast portal signals before method return."""

    def __init__(self) -> None:
        self._responses: dict[str, PortalResponse] = {}
        self._events: dict[str, asyncio.Event] = {}

    def handle_message(self, message: Any) -> bool:
        from dbus_fast.constants import MessageType

        if (
            message.message_type is not MessageType.SIGNAL
            or message.interface != _REQUEST_INTERFACE
            or message.member != "Response"
        ):
            return False
        results = {
            key: getattr(value, "value", value)
            for key, value in message.body[1].items()
        }
        self._responses[message.path] = PortalResponse(
            code=int(message.body[0]), results=results
        )
        event = self._events.get(message.path)
        if event is not None:
            event.set()
        return True

    async def wait(self, request_path: str) -> PortalResponse:
        response = self._responses.get(request_path)
        if response is not None:
            return response
        event = self._events.setdefault(request_path, asyncio.Event())
        await event.wait()
        return self._responses[request_path]


class DbusPortalTransport:
    """Low-level D-Bus transport for portal methods and request signals."""

    def __init__(
        self,
        *,
        open_file=None,
        close_file=None,
    ) -> None:
        self._open_file = open_file or os.open
        self._close_file = close_file or os.close

    def choose_directory(
        self, *, title: str, initial_dir: str | None, parent_window: str
    ) -> PortalResponse:
        return asyncio.run(
            self._choose_directory(
                title=title,
                initial_dir=initial_dir,
                parent_window=parent_window,
            )
        )

    def reveal_path(self, path: str, *, parent_window: str) -> PortalResponse:
        return asyncio.run(self._reveal_path(path, parent_window=parent_window))

    async def _choose_directory(
        self, *, title: str, initial_dir: str | None, parent_window: str
    ) -> PortalResponse:
        from dbus_fast.signature import Variant

        options: dict[str, Any] = {
            "handle_token": Variant("s", _request_token()),
            "modal": Variant("b", True),
            "directory": Variant("b", True),
        }
        if initial_dir:
            current_folder = os.fsencode(os.path.abspath(initial_dir)) + b"\0"
            options["current_folder"] = Variant("ay", current_folder)
        return await self._perform_request(
            interface=_FILE_CHOOSER_INTERFACE,
            member="OpenFile",
            signature="ssa{sv}",
            body=[parent_window, title, options],
            minimum_version=3,
        )

    async def _reveal_path(
        self, path: str, *, parent_window: str
    ) -> PortalResponse:
        from dbus_fast.signature import Variant

        descriptor = self._open_file(
            path, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        )
        try:
            return await self._perform_request(
                interface=_OPEN_URI_INTERFACE,
                member="OpenDirectory",
                signature="sha{sv}",
                body=[
                    parent_window,
                    0,
                    {"handle_token": Variant("s", _request_token())},
                ],
                unix_fds=[descriptor],
                minimum_version=3,
            )
        finally:
            self._close_file(descriptor)

    async def _perform_request(
        self,
        *,
        interface: str,
        member: str,
        signature: str,
        body: list[Any],
        minimum_version: int,
        unix_fds: list[int] | None = None,
    ) -> PortalResponse:
        from dbus_fast.aio import MessageBus

        bus = MessageBus(negotiate_unix_fd=True)
        waiter = _PortalResponseWaiter()
        match_added = False
        try:
            try:
                await bus.connect()
            except Exception as exc:
                raise PortalUnavailableError(
                    f"could not connect to the session D-Bus: {exc}"
                ) from exc
            version = await self._interface_version(bus, interface)
            if version < minimum_version:
                raise PortalUnavailableError(
                    f"{interface} version {version} is older than required "
                    f"version {minimum_version}"
                )
            bus.add_message_handler(waiter.handle_message)
            await self._set_match_rule(bus, "AddMatch")
            match_added = True
            reply = await self._call(
                bus,
                destination=_PORTAL_DESTINATION,
                path=_PORTAL_PATH,
                interface=interface,
                member=member,
                signature=signature,
                body=body,
                unix_fds=unix_fds,
            )
            request_path = str(reply.body[0])
            return await waiter.wait(request_path)
        finally:
            if match_added:
                try:
                    await self._set_match_rule(bus, "RemoveMatch")
                except Exception:
                    pass
            try:
                bus.remove_message_handler(waiter.handle_message)
            except Exception:
                pass
            try:
                bus.disconnect()
            except Exception:
                pass

    async def _interface_version(self, bus: Any, interface: str) -> int:
        reply = await self._call(
            bus,
            destination=_PORTAL_DESTINATION,
            path=_PORTAL_PATH,
            interface="org.freedesktop.DBus.Properties",
            member="Get",
            signature="ss",
            body=[interface, "version"],
        )
        value = getattr(reply.body[0], "value", reply.body[0])
        return int(value)

    async def _set_match_rule(self, bus: Any, member: str) -> None:
        await self._call(
            bus,
            destination="org.freedesktop.DBus",
            path="/org/freedesktop/DBus",
            interface="org.freedesktop.DBus",
            member=member,
            signature="s",
            body=[_RESPONSE_MATCH_RULE],
        )

    @staticmethod
    async def _call(
        bus: Any,
        *,
        destination: str,
        path: str,
        interface: str,
        member: str,
        signature: str,
        body: list[Any],
        unix_fds: list[int] | None = None,
    ) -> Any:
        from dbus_fast.constants import MessageType
        from dbus_fast.message import Message

        reply = await bus.call(
            Message(
                destination=destination,
                path=path,
                interface=interface,
                member=member,
                signature=signature,
                body=body,
                unix_fds=unix_fds or [],
            )
        )
        if reply.message_type is MessageType.ERROR:
            detail = str(reply.body[0]) if reply.body else str(reply.error_name)
            raise PortalUnavailableError(detail)
        return reply


def _request_token() -> str:
    # Portal tokens are D-Bus object-path elements, so keep them alphanumeric.
    return f"caveviewer{secrets.token_hex(8)}"
