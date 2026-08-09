"""Map-opening workflow helpers shared by startup and viewer UI."""

from __future__ import annotations

from dataclasses import dataclass
import os
from typing import TYPE_CHECKING, Any, Mapping

from caveviewer.core.chunking import builder as chunker
from caveviewer.core.capabilities import CapabilityResult, CapabilityStatus
from caveviewer.core.diagnostics.logging import get_logger
from caveviewer.core.map import source_model
from caveviewer.gui.features import (
    FeatureDecision,
    FeatureId,
    decide_map_source_import,
)
from caveviewer.gui.platform import (
    DesktopServices,
    get_desktop_services,
    tk_root_options,
)
from caveviewer.gui.platform.directory_selection import (
    authorized_directory_selection_target,
    choose_authorized_directory,
    directory_selection_preflight,
)
from caveviewer.gui.platform.presentation import get_presentation_profile

if TYPE_CHECKING:
    from caveviewer.gui.platform.runtime import PlatformRuntime


_LOG = get_logger("CaveViewer")


@dataclass(frozen=True)
class OpenMapTarget:
    """Resolved target selected from a map-folder dialog."""

    source_dir: str
    map_name: str
    model_descriptor: dict[str, Any] | None = None
    cache_dir: str | None = None
    textures_dir: str | None = None
    manifest: dict[str, Any] | None = None

    @property
    def is_prebuilt_cache(self) -> bool:
        """Return whether this target is an already-built chunk cache."""
        return self.cache_dir is not None


@dataclass(frozen=True, slots=True)
class MapSourceImportPreflight:
    """One selected source-format fact paired with its import decision.

    A descriptor is mutable action-time input, so the preflight keeps the
    capability fact and policy decision together until map-opening accepts it.
    An executable decision must name exactly the route declared by the
    canonical source-format registry.
    """

    capability: CapabilityResult[source_model.SourceFormat]
    decision: FeatureDecision

    def __post_init__(self) -> None:
        if self.decision.feature is not FeatureId.MAP_SOURCE_IMPORT:
            raise ValueError(
                "map-source-import preflight must contain a map-source-import "
                "decision"
            )
        if not self.decision.allows_execution:
            return
        source_format = self.capability.value
        if (
            self.capability.status is not CapabilityStatus.AVAILABLE
            or not isinstance(source_format, source_model.SourceFormat)
        ):
            raise ValueError(
                "executable map-source-import preflight requires an available "
                "source format"
            )
        if self.decision.route != source_format.id.value:
            raise ValueError(
                "map-source-import decision route must match its source format"
            )


def pick_folder_dialog(
    *,
    desktop_services: DesktopServices | None = None,
    platform_runtime: PlatformRuntime | None = None,
) -> str | None:
    """Open the authorized platform directory chooser for map workflows."""
    if desktop_services is None:
        desktop_services = (
            platform_runtime.desktop_services
            if platform_runtime is not None
            else get_desktop_services()
        )

    preflight = directory_selection_preflight(
        desktop_services,
        platform_runtime=platform_runtime,
    )
    # Validate before creating a hidden Tk root, so a stale or mismatched route
    # cannot cause native chooser setup as a side effect.
    authorized_directory_selection_target(preflight, desktop_services)

    root = (
        _hidden_tk_root()
        if platform_runtime is None
        else _hidden_tk_root(platform_runtime=platform_runtime)
    )
    try:
        selection = choose_authorized_directory(
            preflight,
            desktop_services,
            title="Open Map Folder",
            parent=root,
        )
        return selection.path if selection else None
    finally:
        root.destroy()


def resolve_selected_map_folder(folder: str) -> OpenMapTarget:
    """Resolve a selected folder into a source-map import or prebuilt cache."""
    normalized = os.path.abspath(folder)
    try:
        model_descriptor = source_model.find_model_file(normalized, logger=_LOG)
    except FileNotFoundError as exc:
        return _resolve_prebuilt_cache(normalized, exc)

    source_import_preflight = map_source_import_preflight(model_descriptor)
    if not source_import_preflight.decision.allows_execution:
        raise FileNotFoundError(source_import_preflight.decision.explanation)

    source_path = model_descriptor.get("obj_path") or model_descriptor.get("glb_path")
    map_name = os.path.basename(str(source_path or normalized))
    return OpenMapTarget(
        source_dir=normalized,
        map_name=map_name,
        model_descriptor=model_descriptor,
        textures_dir=normalized,
    )


def map_source_import_decision(
    model_descriptor: Mapping[str, Any],
) -> FeatureDecision:
    """Return the compatibility decision for one already-discovered descriptor."""
    return map_source_import_preflight(model_descriptor).decision


def map_source_import_preflight(
    model_descriptor: Mapping[str, Any],
) -> MapSourceImportPreflight:
    """Pair one descriptor's source-format fact with its import policy.

    This is evaluated immediately before the GUI accepts a discovered source
    map. It does not parse source files or perform an import.
    """
    capability = source_model.probe_model_descriptor(model_descriptor)
    return MapSourceImportPreflight(
        capability=capability,
        decision=decide_map_source_import(capability),
    )


def _resolve_prebuilt_cache(folder: str, no_model_error: FileNotFoundError) -> OpenMapTarget:
    """Resolve a selected folder as a prebuilt cache or re-raise no-map errors."""
    manifest_path = os.path.join(folder, chunker.MANIFEST_NAME)
    if not os.path.exists(manifest_path):
        raise no_model_error

    manifest = chunker.load_manifest(folder)
    map_name = os.path.basename(str(manifest.get("source_obj") or folder))
    return OpenMapTarget(
        source_dir=folder,
        map_name=map_name,
        cache_dir=folder,
        textures_dir=folder,
        manifest=manifest,
    )


def _hidden_tk_root(*, platform_runtime: PlatformRuntime | None = None):
    """Create the hidden Tk owner used for native chooser dialogs."""
    import tkinter as tk

    from caveviewer.gui.dpi_utils import (
        apply_tk_scaling,
        configure_process_dpi_awareness,
    )

    if platform_runtime is None:
        # Preserve the long-standing direct-call seam for CLI helpers and
        # focused tests. The DPI helper resolves its own action facade and
        # pure profile in that compatibility path.
        configure_process_dpi_awareness()
        root = tk.Tk(**tk_root_options())
        apply_tk_scaling(root)
        root.withdraw()
        return root

    presentation_profile = (
        getattr(platform_runtime, "presentation_profile", None)
        or get_presentation_profile()
    )
    presentation_actions_adapter = getattr(
        platform_runtime,
        "presentation_actions_adapter",
        None,
    )
    configure_process_dpi_awareness(
        presentation_actions_adapter=presentation_actions_adapter
    )
    root = tk.Tk(**tk_root_options())
    apply_tk_scaling(root, presentation_profile=presentation_profile)
    root.withdraw()
    return root
