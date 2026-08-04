"""Immutable desktop-action route values shared by platform probes and policy."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class DirectorySelectionRoute(str, Enum):
    """One safe implementation route for choosing a local directory."""

    PORTAL = "portal"
    TK = "tk"
    INJECTED = "injected"


class FileSelectionRoute(str, Enum):
    """One safe implementation route for opening a local file."""

    PORTAL = "portal"
    TK = "tk"
    INJECTED = "injected"


class DesktopNotificationRoute(str, Enum):
    """One route for an optional desktop notification action."""

    PORTAL = "portal"
    INJECTED = "injected"
    NOOP = "noop"


class IdleSuspendInhibitionRoute(str, Enum):
    """One route for an optional scoped idle/suspend inhibitor."""

    PORTAL = "portal"
    INJECTED = "injected"
    NOOP = "noop"


class UpdatePackageRevealRoute(str, Enum):
    """A native route that exposes a verified update without executing it."""

    FINDER = "finder"
    EXPLORER = "explorer"
    DESKTOP_SERVICE = "desktop_service"
    LEGACY_ADAPTER = "legacy_adapter"


@dataclass(frozen=True, slots=True)
class DirectorySelectionTarget:
    """Preferred and fallback routes available for one picker invocation.

    ``PORTAL`` with a ``TK`` fallback models the existing Linux behavior: use
    the host portal first, then keep the map-opening workflow available through
    the portable Tk picker if the portal request cannot be completed.
    """

    primary_route: DirectorySelectionRoute
    fallback_route: DirectorySelectionRoute | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.primary_route, DirectorySelectionRoute):
            raise TypeError("directory-selection primary route must be a known route")
        if self.fallback_route is not None and not isinstance(
            self.fallback_route,
            DirectorySelectionRoute,
        ):
            raise TypeError("directory-selection fallback route must be a known route")
        if self.fallback_route is self.primary_route:
            raise ValueError("directory-selection fallback must differ from primary")

    @property
    def route_key(self) -> str:
        """Return the stable execution-route identifier for a feature decision."""
        if self.fallback_route is None:
            return self.primary_route.value
        return f"{self.primary_route.value}_then_{self.fallback_route.value}"


@dataclass(frozen=True, slots=True)
class FileSelectionTarget:
    """Preferred and fallback routes available for one file-opening action.

    ``PORTAL`` with a ``TK`` fallback models the existing Linux behavior: use
    the host portal first, then keep map-local Guided Dive selection available
    through the portable Tk picker if the portal request cannot be completed.
    """

    primary_route: FileSelectionRoute
    fallback_route: FileSelectionRoute | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.primary_route, FileSelectionRoute):
            raise TypeError("file-selection primary route must be a known route")
        if self.fallback_route is not None and not isinstance(
            self.fallback_route,
            FileSelectionRoute,
        ):
            raise TypeError("file-selection fallback route must be a known route")
        if self.fallback_route is self.primary_route:
            raise ValueError("file-selection fallback must differ from primary")

    @property
    def route_key(self) -> str:
        """Return the stable execution-route identifier for a feature decision."""
        if self.fallback_route is None:
            return self.primary_route.value
        return f"{self.primary_route.value}_then_{self.fallback_route.value}"


@dataclass(frozen=True, slots=True)
class DesktopNotificationTarget:
    """Preferred and fallback routes for one optional notification action.

    ``PORTAL`` with a ``NOOP`` fallback models Linux's existing behavior: try
    the desktop portal, then safely continue when the portable fallback has no
    native notification implementation. A primary ``NOOP`` declaration is
    intentionally not an executable notification capability.
    """

    primary_route: DesktopNotificationRoute
    fallback_route: DesktopNotificationRoute | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.primary_route, DesktopNotificationRoute):
            raise TypeError("desktop-notification primary route must be a known route")
        if self.fallback_route is not None and not isinstance(
            self.fallback_route,
            DesktopNotificationRoute,
        ):
            raise TypeError(
                "desktop-notification fallback route must be a known route"
            )
        if self.fallback_route is self.primary_route:
            raise ValueError("desktop-notification fallback must differ from primary")

    @property
    def route_key(self) -> str:
        """Return the stable execution-route identifier for a feature decision."""
        if self.fallback_route is None:
            return self.primary_route.value
        return f"{self.primary_route.value}_then_{self.fallback_route.value}"


@dataclass(frozen=True, slots=True)
class IdleSuspendInhibitionTarget:
    """Preferred and fallback routes for one scoped optional inhibitor.

    ``PORTAL`` with a ``NOOP`` fallback models the existing Linux behavior:
    request an XDG Portal inhibition handle first, then safely continue if the
    portable fallback has no native inhibition capability. A primary ``NOOP``
    declaration is not an executable inhibitor capability.
    """

    primary_route: IdleSuspendInhibitionRoute
    fallback_route: IdleSuspendInhibitionRoute | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.primary_route, IdleSuspendInhibitionRoute):
            raise TypeError("idle-suspend-inhibition primary route must be a known route")
        if self.fallback_route is not None and not isinstance(
            self.fallback_route,
            IdleSuspendInhibitionRoute,
        ):
            raise TypeError(
                "idle-suspend-inhibition fallback route must be a known route"
            )
        if self.fallback_route is self.primary_route:
            raise ValueError(
                "idle-suspend-inhibition fallback must differ from primary"
            )

    @property
    def route_key(self) -> str:
        """Return the stable execution-route identifier for a feature decision."""
        if self.fallback_route is None:
            return self.primary_route.value
        return f"{self.primary_route.value}_then_{self.fallback_route.value}"
