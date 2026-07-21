"""Map-folder validation shared by the splash and sample-map dialogs."""

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

    if glob.glob(os.path.join(folder, "*.glb")):
        return True, ""

    obj_candidates = glob.glob(os.path.join(folder, "*.obj"))
    if obj_candidates:
        obj_path = obj_candidates[0]
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
            "Found an .obj file, but no matching .mtl file in that folder.\n\n"
            "Select the folder that contains your cave map files: .glb, or "
            ".obj with its matching .mtl and textures."
        )

    if has_precompiled_cache(folder):
        return True, ""
    return False, (
        "No supported map files were found in that folder.\n\n"
        "Select the folder that contains your cave map files: .glb, or "
        ".obj with its matching .mtl and textures."
    )
