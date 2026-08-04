"""Immutable desktop-action route values shared by platform probes and policy."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class DirectorySelectionRoute(str, Enum):
    """One safe implementation route for choosing a local directory."""

    PORTAL = "portal"
    TK = "tk"
    INJECTED = "injected"


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
