"""Core source-model discovery for supported CaveViewer map inputs."""

from __future__ import annotations

import glob
import os
from typing import Any

from caveviewer.core.diagnostics.logging import get_logger


_LOG = get_logger("ModelDiscovery")
_SUPPORTED_EXTENSIONS = (".obj", ".glb")


def _info(logger: Any | None, message: str) -> None:
    target = logger or _LOG
    target.info(message)


def _resolve_companion_path(folder: str, relative_path: str, description: str) -> str:
    raw_path = str(relative_path)
    normalized = os.path.normpath(raw_path)
    if (
        not raw_path.strip()
        or os.path.isabs(normalized)
        or normalized == os.pardir
        or normalized.startswith(os.pardir + os.sep)
    ):
        raise ValueError(f"Unsafe {description} path: {raw_path!r}")

    root = os.path.abspath(folder)
    resolved = os.path.abspath(os.path.join(root, normalized))
    try:
        common = os.path.commonpath((root, resolved))
    except ValueError as exc:
        raise ValueError(f"Unsafe {description} path: {raw_path!r}") from exc
    if common != root:
        raise ValueError(f"Unsafe {description} path: {raw_path!r}")
    return resolved


def find_input_files(folder: str, *, logger: Any | None = None) -> tuple[str, str]:
    """Locate the OBJ and MTL files inside ``folder``."""
    obj_candidates = glob.glob(os.path.join(folder, "*.obj"))
    if not obj_candidates:
        raise FileNotFoundError(
            f"No .obj file found in:\n  {folder}\n\n"
            f"Make sure you selected the folder that contains the exported "
            f".obj, .mtl, and .jpg texture tiles from Agisoft."
        )
    if len(obj_candidates) > 1:
        _info(
            logger,
            f"Note: multiple .obj files found, using the first one: {obj_candidates[0]}",
        )
    obj_path = obj_candidates[0]

    return obj_path, find_material_file_for_obj(obj_path)


def find_material_file_for_obj(obj_path: str) -> str:
    """Return the material file referenced by or adjacent to one OBJ file."""
    folder = os.path.dirname(os.path.abspath(obj_path))

    mtl_name = None
    with open(obj_path, "r", errors="replace") as file_obj:
        for line in file_obj:
            if line.startswith("mtllib "):
                mtl_name = line.split(maxsplit=1)[1].strip()
                break

    if mtl_name:
        mtl_path = _resolve_companion_path(folder, mtl_name, "OBJ material")
        if os.path.exists(mtl_path):
            return mtl_path

    mtl_candidates = glob.glob(os.path.join(folder, "*.mtl"))
    if not mtl_candidates:
        raise FileNotFoundError(
            f"Found {os.path.basename(obj_path)} but no matching .mtl file in:\n  {folder}"
        )
    return mtl_candidates[0]


def find_model_file(folder: str, *, logger: Any | None = None) -> dict:
    """
    Detect a supported model input and return a descriptor dictionary.

    Returns one of:

    - ``{"format": "obj", "obj_path": ..., "mtl_path": ...}``
    - ``{"format": "glb", "glb_path": ...}``
    """
    selected_path = os.path.abspath(folder)
    if os.path.isfile(selected_path):
        ext = os.path.splitext(selected_path)[1].lower()
        if ext == ".obj":
            return {
                "format": "obj",
                "obj_path": selected_path,
                "mtl_path": find_material_file_for_obj(selected_path),
            }
        if ext == ".glb":
            return {"format": "glb", "glb_path": selected_path}
        raise FileNotFoundError(
            f"No supported model file found at:\n  {selected_path}\n\n"
            f"CaveViewer supports .obj (with a matching .mtl) and .glb files."
        )

    folder = selected_path
    for ext in _SUPPORTED_EXTENSIONS:
        candidates = glob.glob(os.path.join(folder, f"*{ext}"))
        if not candidates:
            continue
        if len(candidates) > 1:
            _info(logger, f"Note: multiple {ext} files found, using the first one: {candidates[0]}")
        model_path = candidates[0]

        if ext == ".obj":
            obj_path, mtl_path = find_input_files(folder, logger=logger)
            return {"format": "obj", "obj_path": obj_path, "mtl_path": mtl_path}
        if ext == ".glb":
            return {"format": "glb", "glb_path": model_path}

    raise FileNotFoundError(
        f"No supported model file found in:\n  {folder}\n\n"
        f"CaveViewer supports .obj (with a matching .mtl) and .glb files. "
        f"Make sure you selected the folder containing your exported map."
    )
