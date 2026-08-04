"""Map-opening workflow helpers shared by startup and viewer UI."""

from __future__ import annotations

from dataclasses import dataclass
import os
from typing import Any

from caveviewer.core.chunking import builder as chunker
from caveviewer.core.diagnostics.logging import get_logger
from caveviewer.core.map import source_model
from caveviewer.gui.features import FeatureDecision, decide_map_source_import
from caveviewer.gui.platform import DesktopServices, get_desktop_services, tk_root_options


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


def pick_folder_dialog(*, desktop_services: DesktopServices | None = None) -> str | None:
    """Open the platform directory chooser used by folder/cache workflows."""
    root = _hidden_tk_root()
    try:
        services = desktop_services or get_desktop_services()
        selection = services.choose_directory(
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

    source_import_decision = map_source_import_decision(model_descriptor)
    if not source_import_decision.allows_execution:
        raise FileNotFoundError(source_import_decision.explanation)

    source_path = model_descriptor.get("obj_path") or model_descriptor.get("glb_path")
    map_name = os.path.basename(str(source_path or normalized))
    return OpenMapTarget(
        source_dir=normalized,
        map_name=map_name,
        model_descriptor=model_descriptor,
        textures_dir=normalized,
    )


def map_source_import_decision(
    model_descriptor: dict[str, Any],
) -> FeatureDecision:
    """Evaluate the action-time gate for one already-discovered map descriptor."""
    return decide_map_source_import(source_model.probe_model_descriptor(model_descriptor))


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


def _hidden_tk_root():
    """Create the hidden Tk owner used for native chooser dialogs."""
    import tkinter as tk

    from caveviewer.gui.dpi_utils import (
        apply_tk_scaling,
        configure_process_dpi_awareness,
    )

    configure_process_dpi_awareness()
    root = tk.Tk(**tk_root_options())
    apply_tk_scaling(root)
    root.withdraw()
    return root
