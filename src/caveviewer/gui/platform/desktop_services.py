"""Cross-platform file-selection and file-manager capabilities.

UI modules depend on this narrow interface instead of importing Tk or Linux
desktop APIs directly.  That keeps portal policy in the platform layer and
also gives tests a deterministic injection point.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import unquote, urlsplit


class DesktopServiceError(RuntimeError):
    """A desktop integration request could not be completed."""


@dataclass(frozen=True)
class DirectorySelection:
    """A selected local directory and its source URI."""

    path: str
    uri: str

    @classmethod
    def from_path(cls, path: str) -> "DirectorySelection":
        normalized = os.path.abspath(os.path.expanduser(path))
        return cls(path=normalized, uri=Path(normalized).as_uri())

    @classmethod
    def from_uri(cls, uri: str) -> "DirectorySelection":
        parsed = urlsplit(uri)
        if parsed.scheme != "file" or parsed.netloc not in {"", "localhost"}:
            raise DesktopServiceError(
                f"The desktop portal returned an unsupported directory URI: {uri}"
            )
        return cls.from_path(unquote(parsed.path))


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


def get_desktop_services() -> DesktopServices:
    """Return the desktop integration selected for this operating system."""
    fallback = TkDesktopServices()
    if sys.platform.startswith("linux"):
        # Import lazily so non-Linux bundles do not load the D-Bus transport.
        from caveviewer.gui.platform.portal import LinuxPortalDesktopServices

        return LinuxPortalDesktopServices(fallback=fallback)
    return fallback
