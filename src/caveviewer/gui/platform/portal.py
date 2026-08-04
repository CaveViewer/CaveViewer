"""XDG Desktop Portal file selection and reveal integration for Linux."""

from __future__ import annotations

import asyncio
import concurrent.futures
import enum
import os
import secrets
import subprocess
import threading
from dataclasses import dataclass, field
from types import TracebackType
from typing import Any, Protocol
from urllib.parse import unquote, urlsplit

from caveviewer.core.capabilities import (
    DirectorySelectionRoute,
    DirectorySelectionTarget,
)
from caveviewer.core.diagnostics.logging import get_logger
from caveviewer.gui.platform.desktop_services import (
    DesktopInhibitor,
    DesktopServiceError,
    DesktopServices,
    DirectorySelection,
    FileSelection,
    NoopDesktopInhibitor,
)
from caveviewer.version import APP_NAME


_LOG = get_logger("CaveViewer")
_PORTAL_DESTINATION = "org.freedesktop.portal.Desktop"
_PORTAL_PATH = "/org/freedesktop/portal/desktop"
_REQUEST_INTERFACE = "org.freedesktop.portal.Request"
_FILE_CHOOSER_INTERFACE = "org.freedesktop.portal.FileChooser"
_OPEN_URI_INTERFACE = "org.freedesktop.portal.OpenURI"
_NOTIFICATION_INTERFACE = "org.freedesktop.portal.Notification"
_INHIBIT_INTERFACE = "org.freedesktop.portal.Inhibit"
_RESPONSE_MATCH_RULE = (
    "type='signal',interface='org.freedesktop.portal.Request',member='Response'"
)
_INHIBIT_SUSPEND = 4
_INHIBIT_IDLE = 8


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

    def choose_file(
        self, *, title: str, initial_dir: str | None, parent_window: str
    ) -> PortalResponse:
        ...

    def save_file(
        self,
        *,
        title: str,
        initial_dir: str | None,
        initial_name: str | None,
        parent_window: str,
    ) -> PortalResponse:
        ...

    def open_uri(self, uri: str, *, parent_window: str) -> PortalResponse:
        ...

    def open_path(self, path: str, *, parent_window: str) -> PortalResponse:
        ...

    def notify(
        self,
        notification_id: str,
        title: str,
        body: str,
        *,
        priority: str,
    ) -> None:
        ...

    def withdraw_notification(self, notification_id: str) -> None:
        ...

    def inhibit(
        self, *, reason: str, parent_window: str, flags: int
    ) -> DesktopInhibitor:
        ...


class XdgPortalClient:
    """Translate typed desktop requests into portal response states."""

    def __init__(self, transport: PortalTransport | None = None) -> None:
        self._transport = transport or DbusPortalTransport()
        self.last_state = PortalRequestState.IDLE

    def choose_directory(
        self, *, title: str, initial_dir: str | None, parent_window: str
    ) -> DirectorySelection | None:
        response = self._run_response_request(
            "directory",
            lambda: self._transport.choose_directory(
                title=title,
                initial_dir=initial_dir,
                parent_window=parent_window,
            ),
        )
        if response is None:
            return None
        try:
            return DirectorySelection.from_uri(
                _single_response_uri(response, "directory")
            )
        except Exception:
            self.last_state = PortalRequestState.FAILED
            raise

    def choose_file(
        self, *, title: str, initial_dir: str | None, parent_window: str
    ) -> FileSelection | None:
        response = self._run_response_request(
            "file open",
            lambda: self._transport.choose_file(
                title=title,
                initial_dir=initial_dir,
                parent_window=parent_window,
            ),
        )
        if response is None:
            return None
        try:
            return FileSelection.from_uri(_single_response_uri(response, "file"))
        except Exception:
            self.last_state = PortalRequestState.FAILED
            raise

    def save_file(
        self,
        *,
        title: str,
        initial_dir: str | None,
        initial_name: str | None,
        parent_window: str,
    ) -> FileSelection | None:
        response = self._run_response_request(
            "file save",
            lambda: self._transport.save_file(
                title=title,
                initial_dir=initial_dir,
                initial_name=initial_name,
                parent_window=parent_window,
            ),
        )
        if response is None:
            return None
        try:
            return FileSelection.from_uri(_single_response_uri(response, "file"))
        except Exception:
            self.last_state = PortalRequestState.FAILED
            raise

    def open_uri(self, uri: str, *, parent_window: str) -> None:
        self._run_response_request(
            "open URI",
            lambda: self._transport.open_uri(uri, parent_window=parent_window),
        )

    def open_path(self, path: str, *, parent_window: str) -> None:
        self._run_response_request(
            "open path",
            lambda: self._transport.open_path(path, parent_window=parent_window),
        )

    def notify(
        self,
        notification_id: str,
        title: str,
        body: str,
        *,
        priority: str = "normal",
    ) -> None:
        self._transport.notify(
            notification_id, title, body, priority=priority
        )

    def withdraw_notification(self, notification_id: str) -> None:
        self._transport.withdraw_notification(notification_id)

    def inhibit_idle_suspend(
        self, *, reason: str, parent_window: str
    ) -> DesktopInhibitor:
        return self._transport.inhibit(
            reason=reason,
            parent_window=parent_window,
            flags=_INHIBIT_IDLE | _INHIBIT_SUSPEND,
        )

    def _run_response_request(
        self, description: str, request: Any
    ) -> PortalResponse | None:
        machine = PortalRequestStateMachine()
        machine.transition(PortalRequestState.REQUESTING)
        try:
            machine.transition(PortalRequestState.WAITING)
            response = request()
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
                f"The desktop portal failed the {description} request ({response.code})."
            )
        machine.transition(PortalRequestState.COMPLETED)
        self.last_state = machine.state
        return response

    def reveal_path(self, path: str, *, parent_window: str) -> None:
        self._run_response_request(
            "reveal",
            lambda: self._transport.reveal_path(
                path, parent_window=parent_window
            ),
        )


def _single_response_uri(response: PortalResponse, selection_label: str) -> str:
    uris = response.results.get("uris") or ()
    if not uris:
        raise PortalRequestError(
            f"The desktop portal completed without returning a {selection_label}."
        )
    return str(uris[0])


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

    def directory_selection_target(self) -> DirectorySelectionTarget:
        """Declare the Portal-first route and its portable Tk fallback."""
        return DirectorySelectionTarget(
            primary_route=DirectorySelectionRoute.PORTAL,
            fallback_route=DirectorySelectionRoute.TK,
        )

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

    def choose_file(
        self,
        *,
        title: str,
        initial_dir: str | None = None,
        parent: Any | None = None,
    ) -> FileSelection | None:
        try:
            return self._portal.choose_file(
                title=title,
                initial_dir=initial_dir,
                parent_window=portal_parent_window(parent),
            )
        except Exception as exc:
            _LOG.warning(f"Desktop portal file chooser unavailable: {exc}")
            return self._fallback.choose_file(
                title=title, initial_dir=initial_dir, parent=parent
            )

    def save_file(
        self,
        *,
        title: str,
        initial_dir: str | None = None,
        initial_name: str | None = None,
        parent: Any | None = None,
    ) -> FileSelection | None:
        try:
            return self._portal.save_file(
                title=title,
                initial_dir=initial_dir,
                initial_name=initial_name,
                parent_window=portal_parent_window(parent),
            )
        except Exception as exc:
            _LOG.warning(f"Desktop portal save dialog unavailable: {exc}")
            return self._fallback.save_file(
                title=title,
                initial_dir=initial_dir,
                initial_name=initial_name,
                parent=parent,
            )

    def open_uri(self, uri: str, *, parent: Any | None = None) -> None:
        parsed = urlsplit(uri)
        if parsed.scheme == "file":
            self.open_path(unquote(parsed.path), parent=parent)
            return
        try:
            self._portal.open_uri(uri, parent_window=portal_parent_window(parent))
            return
        except Exception as exc:
            _LOG.warning(f"Desktop portal URI open unavailable: {exc}")
        self._fallback.open_uri(uri, parent=parent)

    def open_path(self, path: str, *, parent: Any | None = None) -> None:
        try:
            self._portal.open_path(
                os.path.abspath(path),
                parent_window=portal_parent_window(parent),
            )
            return
        except Exception as exc:
            _LOG.warning(f"Desktop portal file open unavailable: {exc}")
        subprocess.Popen(["xdg-open", os.path.abspath(os.path.expanduser(path))])

    def notify(
        self,
        notification_id: str,
        title: str,
        body: str = "",
        *,
        priority: str = "normal",
    ) -> None:
        try:
            self._portal.notify(
                notification_id, title, body, priority=priority
            )
        except Exception as exc:
            _LOG.warning(f"Desktop portal notification unavailable: {exc}")
            self._fallback.notify(
                notification_id, title, body, priority=priority
            )

    def withdraw_notification(self, notification_id: str) -> None:
        try:
            self._portal.withdraw_notification(notification_id)
        except Exception as exc:
            _LOG.warning(f"Desktop portal notification withdraw unavailable: {exc}")
            self._fallback.withdraw_notification(notification_id)

    def inhibit_idle_suspend(
        self, reason: str, *, parent: Any | None = None
    ) -> DesktopInhibitor:
        try:
            return self._portal.inhibit_idle_suspend(
                reason=reason,
                parent_window=portal_parent_window(parent),
            )
        except Exception as exc:
            _LOG.warning(f"Desktop portal inhibit unavailable: {exc}")
            return self._fallback.inhibit_idle_suspend(reason, parent=parent)


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

    def choose_file(
        self, *, title: str, initial_dir: str | None, parent_window: str
    ) -> PortalResponse:
        return asyncio.run(
            self._choose_file(
                title=title,
                initial_dir=initial_dir,
                parent_window=parent_window,
            )
        )

    def save_file(
        self,
        *,
        title: str,
        initial_dir: str | None,
        initial_name: str | None,
        parent_window: str,
    ) -> PortalResponse:
        return asyncio.run(
            self._save_file(
                title=title,
                initial_dir=initial_dir,
                initial_name=initial_name,
                parent_window=parent_window,
            )
        )

    def open_uri(self, uri: str, *, parent_window: str) -> PortalResponse:
        return asyncio.run(self._open_uri(uri, parent_window=parent_window))

    def open_path(self, path: str, *, parent_window: str) -> PortalResponse:
        return asyncio.run(self._open_path(path, parent_window=parent_window))

    def notify(
        self,
        notification_id: str,
        title: str,
        body: str,
        *,
        priority: str,
    ) -> None:
        asyncio.run(
            self._notify(
                notification_id,
                title,
                body,
                priority=priority,
            )
        )

    def withdraw_notification(self, notification_id: str) -> None:
        asyncio.run(self._withdraw_notification(notification_id))

    def inhibit(
        self, *, reason: str, parent_window: str, flags: int
    ) -> DesktopInhibitor:
        return self._start_inhibit_thread(
            reason=reason, parent_window=parent_window, flags=flags
        )

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
            options["current_folder"] = Variant(
                "ay", _nul_terminated_path(initial_dir)
            )
        return await self._perform_request(
            interface=_FILE_CHOOSER_INTERFACE,
            member="OpenFile",
            signature="ssa{sv}",
            body=[parent_window, title, options],
            minimum_version=3,
        )

    async def _choose_file(
        self, *, title: str, initial_dir: str | None, parent_window: str
    ) -> PortalResponse:
        from dbus_fast.signature import Variant

        options: dict[str, Any] = {
            "handle_token": Variant("s", _request_token()),
            "modal": Variant("b", True),
        }
        if initial_dir:
            options["current_folder"] = Variant(
                "ay", _nul_terminated_path(initial_dir)
            )
        return await self._perform_request(
            interface=_FILE_CHOOSER_INTERFACE,
            member="OpenFile",
            signature="ssa{sv}",
            body=[parent_window, title, options],
            minimum_version=1,
        )

    async def _save_file(
        self,
        *,
        title: str,
        initial_dir: str | None,
        initial_name: str | None,
        parent_window: str,
    ) -> PortalResponse:
        from dbus_fast.signature import Variant

        options: dict[str, Any] = {
            "handle_token": Variant("s", _request_token()),
            "modal": Variant("b", True),
        }
        if initial_dir:
            options["current_folder"] = Variant(
                "ay", _nul_terminated_path(initial_dir)
            )
        if initial_name:
            options["current_name"] = Variant("s", initial_name)
        return await self._perform_request(
            interface=_FILE_CHOOSER_INTERFACE,
            member="SaveFile",
            signature="ssa{sv}",
            body=[parent_window, title, options],
            minimum_version=1,
        )

    async def _open_uri(
        self, uri: str, *, parent_window: str
    ) -> PortalResponse:
        from dbus_fast.signature import Variant

        return await self._perform_request(
            interface=_OPEN_URI_INTERFACE,
            member="OpenURI",
            signature="ssa{sv}",
            body=[
                parent_window,
                uri,
                {"handle_token": Variant("s", _request_token())},
            ],
            minimum_version=1,
        )

    async def _open_path(
        self, path: str, *, parent_window: str
    ) -> PortalResponse:
        from dbus_fast.signature import Variant

        descriptor = self._open_file(
            path, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        )
        try:
            return await self._perform_request(
                interface=_OPEN_URI_INTERFACE,
                member="OpenFile",
                signature="sha{sv}",
                body=[
                    parent_window,
                    0,
                    {"handle_token": Variant("s", _request_token())},
                ],
                unix_fds=[descriptor],
                minimum_version=2,
            )
        finally:
            self._close_file(descriptor)

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

    async def _notify(
        self,
        notification_id: str,
        title: str,
        body: str,
        *,
        priority: str,
    ) -> None:
        from dbus_fast.signature import Variant

        notification: dict[str, Any] = {
            "title": Variant("s", title or APP_NAME),
        }
        if body:
            notification["body"] = Variant("s", body)
        if priority:
            notification["priority"] = Variant("s", priority)
        await self._perform_call(
            interface=_NOTIFICATION_INTERFACE,
            member="AddNotification",
            signature="sa{sv}",
            body=[notification_id, notification],
            minimum_version=1,
        )

    async def _withdraw_notification(self, notification_id: str) -> None:
        await self._perform_call(
            interface=_NOTIFICATION_INTERFACE,
            member="RemoveNotification",
            signature="s",
            body=[notification_id],
            minimum_version=1,
        )

    def _start_inhibit_thread(
        self, *, reason: str, parent_window: str, flags: int
    ) -> DesktopInhibitor:
        result: concurrent.futures.Future[DesktopInhibitor] = (
            concurrent.futures.Future()
        )

        def worker() -> None:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)

            async def start() -> None:
                from dbus_fast.aio import MessageBus

                bus = MessageBus(negotiate_unix_fd=False)
                try:
                    await bus.connect()
                    version = await self._interface_version(
                        bus, _INHIBIT_INTERFACE
                    )
                    if version < 1:
                        raise PortalUnavailableError(
                            f"{_INHIBIT_INTERFACE} version {version} is older "
                            "than required version 1"
                        )
                    request_path = await self._start_inhibit(
                        bus,
                        reason=reason,
                        parent_window=parent_window,
                        flags=flags,
                    )
                    result.set_result(
                        _DbusPortalInhibitor(loop, bus, request_path, thread)
                    )
                except Exception as exc:
                    try:
                        bus.disconnect()
                    except Exception:
                        pass
                    result.set_exception(exc)
                    loop.stop()

            loop.create_task(start())
            try:
                loop.run_forever()
            finally:
                loop.close()

        thread = threading.Thread(
            target=worker, name="caveviewer-portal-inhibit", daemon=True
        )
        thread.start()
        try:
            return result.result(timeout=5.0)
        except concurrent.futures.TimeoutError as exc:
            raise PortalUnavailableError(
                "the desktop portal did not return an inhibit handle"
            ) from exc

    async def _start_inhibit(
        self, bus: Any, *, reason: str, parent_window: str, flags: int
    ) -> str:
        from dbus_fast.signature import Variant

        reply = await self._call(
            bus,
            destination=_PORTAL_DESTINATION,
            path=_PORTAL_PATH,
            interface=_INHIBIT_INTERFACE,
            member="Inhibit",
            signature="sua{sv}",
            body=[
                parent_window,
                int(flags),
                {
                    "handle_token": Variant("s", _request_token()),
                    "reason": Variant("s", reason),
                },
            ],
        )
        return str(reply.body[0])

    async def _perform_call(
        self,
        *,
        interface: str,
        member: str,
        signature: str,
        body: list[Any],
        minimum_version: int,
        unix_fds: list[int] | None = None,
    ) -> None:
        from dbus_fast.aio import MessageBus

        bus = MessageBus(negotiate_unix_fd=bool(unix_fds))
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
            await self._call(
                bus,
                destination=_PORTAL_DESTINATION,
                path=_PORTAL_PATH,
                interface=interface,
                member=member,
                signature=signature,
                body=body,
                unix_fds=unix_fds,
            )
        finally:
            try:
                bus.disconnect()
            except Exception:
                pass

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


class _DbusPortalInhibitor:
    """Scoped inhibitor backed by a live portal D-Bus request handle."""

    def __init__(
        self, loop: asyncio.AbstractEventLoop, bus: Any, request_path: str, thread: threading.Thread
    ) -> None:
        self._loop = loop
        self._bus = bus
        self._request_path = request_path
        self._thread = thread
        self._closed = False

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        future = asyncio.run_coroutine_threadsafe(self._close_async(), self._loop)
        try:
            future.result(timeout=3.0)
        finally:
            self._loop.call_soon_threadsafe(self._loop.stop)
            if self._thread is not threading.current_thread():
                self._thread.join(timeout=3.0)

    async def _close_async(self) -> None:
        try:
            await DbusPortalTransport._call(
                self._bus,
                destination=_PORTAL_DESTINATION,
                path=self._request_path,
                interface=_REQUEST_INTERFACE,
                member="Close",
                signature="",
                body=[],
            )
        finally:
            try:
                self._bus.disconnect()
            except Exception:
                pass

    def __enter__(self) -> "_DbusPortalInhibitor":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()


def _nul_terminated_path(path: str) -> bytes:
    return os.fsencode(os.path.abspath(os.path.expanduser(path))) + b"\0"


def _request_token() -> str:
    # Portal tokens are D-Bus object-path elements, so keep them alphanumeric.
    return f"caveviewer{secrets.token_hex(8)}"
