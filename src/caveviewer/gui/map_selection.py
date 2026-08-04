"""Map-folder validation shared by the splash and map-library dialogs."""

from __future__ import annotations

import glob
import os

from caveviewer.core.map import source_model


def has_precompiled_cache(folder: str) -> bool:
    try:
        from caveviewer.core.chunking import builder as chunker
    except Exception:
        return False

    return os.path.exists(os.path.join(folder, chunker.MANIFEST_NAME))


def validate_selected_map_folder(folder: str) -> tuple[bool, str]:
    if not folder or not os.path.isdir(folder):
        return False, "The selected path is not a valid folder."

    candidates = source_model.find_supported_source_files(folder)
    if any(
        candidate.source_format.companion_file_extension is None
        for candidate in candidates
    ):
        return True, ""

    material_candidate = next(iter(candidates), None)
    if material_candidate is not None:
        source_format = material_candidate.source_format
        obj_path = material_candidate.path
        try:
            mtl_path = source_model.find_declared_material_file_for_obj(obj_path)
        except ValueError as exc:
            return False, f"{exc}\n\nSelect a map folder with materials inside it."
        except OSError:
            mtl_path = None

        if mtl_path:
            return True, ""
        if glob.glob(os.path.join(folder, "*.mtl")):
            return True, ""
        if has_precompiled_cache(folder):
            return True, ""
        return False, (
            f"Found a {source_format.extension} file, but no matching "
            f"{source_format.companion_file_extension} file in that folder.\n\n"
            f"{_supported_map_folder_guidance()}"
        )

    if has_precompiled_cache(folder):
        return True, ""
    return False, (
        "No supported map files were found in that folder.\n\n"
        f"{_supported_map_folder_guidance()}"
    )


def _supported_map_folder_guidance() -> str:
    """Build map-picker guidance from the core source-format registry."""
    return (
        "Select the folder that contains your cave map files: "
        f"{source_model.supported_source_format_summary(conjunction='or')}."
    )
