"""Authorize typed viewer-launch routes before native window execution."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING

from caveviewer.core.capabilities import CapabilityStatus, ViewerLaunchTarget
from caveviewer.core.diagnostics.logging import get_logger
from caveviewer.gui.features import decide_viewer_launch

from .probes.windowing import probe_viewer_launch
from .runtime import ViewerLaunchPreflight

if TYPE_CHECKING:
    from .runtime import PlatformRuntime


_LOG = get_logger("ViewerLaunch")


class ViewerLaunchError(RuntimeError):
    """The viewer window cannot start through the selected platform route."""


def viewer_launch_preflight(
    *,
    platform_runtime: PlatformRuntime | None = None,
    environ: Mapping[str, str] | None = None,
    platform_name: str | None = None,
) -> ViewerLaunchPreflight:
    """Return one fresh, side-effect-free viewer-launch preflight.

    The process-owned runtime provides its on-demand preflight for normal GUI
    launches. Compatibility callers and focused tests can inject environment
    facts and use the same probe/policy pair directly. Neither path initializes
    GLFW, creates a native window, or creates a rendering context.
    """
    if platform_runtime is not None and environ is None and platform_name is None:
        runtime_preflight = getattr(platform_runtime, "viewer_launch_preflight", None)
        if callable(runtime_preflight):
            return runtime_preflight()

    capability = probe_viewer_launch(
        environ=environ,
        platform_name=platform_name,
    )
    return ViewerLaunchPreflight(
        capability=capability,
        decision=decide_viewer_launch(capability),
    )


def authorized_viewer_launch_target(
    preflight: ViewerLaunchPreflight,
    *,
    environ: Mapping[str, str] | None = None,
    platform_name: str | None = None,
) -> ViewerLaunchTarget:
    """Return the executable target after rechecking mutable launch facts.

    A decision route is presentation data. The typed target is the authority
    that will be handed to the executor. Re-probing just before native launch
    prevents a display/session change from running an older selected route.
    """
    decision = preflight.decision
    if not decision.allows_execution:
        raise ViewerLaunchError(decision.explanation)

    target = preflight.capability.value
    if (
        preflight.capability.status is not CapabilityStatus.AVAILABLE
        or not isinstance(target, ViewerLaunchTarget)
        or decision.route != target.route_key
    ):
        _LOG.warning(
            "Viewer-launch preflight has no executable target: reason=%s route=%s",
            decision.reason_code,
            decision.route,
        )
        raise ViewerLaunchError("The viewer window is unavailable in this environment.")

    current_capability = probe_viewer_launch(
        environ=environ,
        platform_name=platform_name,
    )
    current_target = current_capability.value
    if (
        current_capability.status is not CapabilityStatus.AVAILABLE
        or not isinstance(current_target, ViewerLaunchTarget)
    ):
        current_decision = decide_viewer_launch(current_capability)
        _LOG.warning(
            "Viewer-launch target is no longer executable: reason=%s",
            current_decision.reason_code,
        )
        raise ViewerLaunchError(current_decision.explanation)
    if current_target != target:
        _LOG.warning(
            "Viewer-launch route changed before execution: selected=%s current=%s",
            target.route_key,
            current_target.route_key,
        )
        raise ViewerLaunchError(
            "Viewer-window availability changed. Return to the map library and try again."
        )
    return target
