"""Map-folder validation shared by the splash and sample-map dialogs."""

from __future__ import annotations

import glob
import os


def has_precompiled_cache(folder: str) -> bool:
    try:
        from caveviewer.core import chunker
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
        mtl_name = None
        try:
            with open(obj_path, "r", errors="replace") as file_obj:
                for line in file_obj:
                    if line.startswith("mtllib "):
                        mtl_name = line.split(maxsplit=1)[1].strip()
                        break
        except Exception:
            pass

        if mtl_name and os.path.exists(os.path.join(folder, mtl_name)):
            return True, ""
        if glob.glob(os.path.join(folder, "*.mtl")):
            return True, ""
        if has_precompiled_cache(folder):
            return True, ""
        return False, (
            "Found an .obj file, but no matching .mtl file in that folder.\n\n"
            "Select a folder with a .glb file, or with both .obj and .mtl files, "
            "or a folder that already contains a CaveViewer pre-compiled cache."
        )

    if has_precompiled_cache(folder):
        return True, ""
    return False, (
        "No supported map files were found in that folder.\n\n"
        "Select a folder with a .glb file, or with both .obj and .mtl files, "
        "or a folder that already contains a CaveViewer pre-compiled cache."
    )
