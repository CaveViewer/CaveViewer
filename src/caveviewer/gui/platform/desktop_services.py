"""Cross-platform file-selection and file-manager capabilities.

UI modules depend on this narrow interface instead of importing Tk or Linux
desktop APIs directly.  That keeps portal policy in the platform layer and
also gives tests a deterministic injection point.
"""

from __future__ import annotations

import os
import subprocess
import sys
import webbrowser
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Any, Protocol
from urllib.parse import unquote, urlsplit


class DesktopServiceError(RuntimeError):
    """A desktop integration request could not be completed."""


@dataclass(frozen=True)
class FileSelection:
    """A selected local filesystem path and its source URI."""

    path: str
    uri: str

    @classmethod
    def from_path(cls, path: str) -> "FileSelection":
        normalized = os.path.abspath(os.path.expanduser(path))
        return cls(path=normalized, uri=Path(normalized).as_uri())

    @classmethod
    def from_uri(cls, uri: str) -> "FileSelection":
        parsed = urlsplit(uri)
        if parsed.scheme != "file" or parsed.netloc not in {"", "localhost"}:
            raise DesktopServiceError(
                f"The desktop portal returned an unsupported local file URI: {uri}"
            )
        path = unquote(parsed.path)
        if os.name == "nt" and _starts_with_windows_drive_uri_path(path):
            path = path[1:].replace("/", "\\")
        return cls.from_path(path)


def _starts_with_windows_drive_uri_path(path: str) -> bool:
    return (
        len(path) >= 3
        and path[0] == "/"
        and path[1].isalpha()
        and path[2] == ":"
    )


class DirectorySelection(FileSelection):
    """A selected local directory and its source URI."""


class DesktopInhibitor(Protocol):
    """A scoped desktop idle/suspend inhibition."""

    def close(self) -> None:
        ...

    def __enter__(self) -> "DesktopInhibitor":
        ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        ...


class NoopDesktopInhibitor:
    """Fallback inhibitor used when no desktop backend is available."""

    def close(self) -> None:
        return

    def __enter__(self) -> "NoopDesktopInhibitor":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()


class DesktopServices(Protocol):
    """Capabilities used to interact with the host desktop."""

    def choose_directory(
        self,
        *,
        title: str,
        initial_dir: str | None = None,
        parent: Any | None = None,
    ) -> DirectorySelection | None:
        ...

    def reveal_path(self, path: str, *, parent: Any | None = None) -> None:
        ...

    def choose_file(
        self,
        *,
        title: str,
        initial_dir: str | None = None,
        parent: Any | None = None,
    ) -> FileSelection | None:
        ...

    def save_file(
        self,
        *,
        title: str,
        initial_dir: str | None = None,
        initial_name: str | None = None,
        parent: Any | None = None,
    ) -> FileSelection | None:
        ...

    def open_uri(self, uri: str, *, parent: Any | None = None) -> None:
        ...

    def open_path(self, path: str, *, parent: Any | None = None) -> None:
        ...

    def notify(
        self,
        notification_id: str,
        title: str,
        body: str = "",
        *,
        priority: str = "normal",
    ) -> None:
        ...

    def withdraw_notification(self, notification_id: str) -> None:
        ...

    def inhibit_idle_suspend(
        self, reason: str, *, parent: Any | None = None
    ) -> DesktopInhibitor:
        ...


class TkDesktopServices:
    """Portable chooser fallback used outside Linux portals."""

    def choose_directory(
        self,
        *,
        title: str,
        initial_dir: str | None = None,
        parent: Any | None = None,
    ) -> DirectorySelection | None:
        from tkinter import filedialog

        options: dict[str, Any] = {"title": title}
        if initial_dir:
            options["initialdir"] = initial_dir
        if parent is not None:
            options["parent"] = parent
        selected = filedialog.askdirectory(**options)
        return DirectorySelection.from_path(selected) if selected else None

    def reveal_path(self, path: str, *, parent: Any | None = None) -> None:
        del parent
        raise DesktopServiceError(
            f"Revealing files is unsupported by the default desktop service: {path}"
        )

    def choose_file(
        self,
        *,
        title: str,
        initial_dir: str | None = None,
        parent: Any | None = None,
    ) -> FileSelection | None:
        from tkinter import filedialog

        options: dict[str, Any] = {"title": title}
        if initial_dir:
            options["initialdir"] = initial_dir
        if parent is not None:
            options["parent"] = parent
        selected = filedialog.askopenfilename(**options)
        return FileSelection.from_path(selected) if selected else None

    def save_file(
        self,
        *,
        title: str,
        initial_dir: str | None = None,
        initial_name: str | None = None,
        parent: Any | None = None,
    ) -> FileSelection | None:
        from tkinter import filedialog

        options: dict[str, Any] = {"title": title}
        if initial_dir:
            options["initialdir"] = initial_dir
        if initial_name:
            options["initialfile"] = initial_name
        if parent is not None:
            options["parent"] = parent
        selected = filedialog.asksaveasfilename(**options)
        return FileSelection.from_path(selected) if selected else None

    def open_uri(self, uri: str, *, parent: Any | None = None) -> None:
        del parent
        if not webbrowser.open(uri):
            raise DesktopServiceError(f"Could not open URI: {uri}")

    def open_path(self, path: str, *, parent: Any | None = None) -> None:
        del parent
        normalized = os.path.abspath(os.path.expanduser(path))
        if sys.platform.startswith("win"):
            os.startfile(normalized)  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", normalized])
        else:
            subprocess.Popen(["xdg-open", normalized])

    def notify(
        self,
        notification_id: str,
        title: str,
        body: str = "",
        *,
        priority: str = "normal",
    ) -> None:
        del notification_id, title, body, priority

    def withdraw_notification(self, notification_id: str) -> None:
        del notification_id

    def inhibit_idle_suspend(
        self, reason: str, *, parent: Any | None = None
    ) -> DesktopInhibitor:
        del reason, parent
        return NoopDesktopInhibitor()


def get_desktop_services(*, platform_name: str | None = None) -> DesktopServices:
    """Return desktop integration for an operating system selected by the caller.

    ``platform_name`` is an injectable composition-time fact.  The default
    retains the historical behavior of using the running interpreter's
    platform, while ``PlatformRuntime`` can compose one shared service for its
    adapter and GUI clients without a global singleton.
    """
    fallback = TkDesktopServices()
    resolved_platform_name = platform_name or sys.platform
    if resolved_platform_name.startswith("linux"):
        # Import lazily so non-Linux bundles do not load the D-Bus transport.
        from caveviewer.gui.platform.portal import LinuxPortalDesktopServices

        return LinuxPortalDesktopServices(fallback=fallback)
    return fallback
