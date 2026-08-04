"""Core source-model discovery for supported CaveViewer map inputs."""

from __future__ import annotations

import glob
import os
from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping

from caveviewer.core.capabilities import CapabilityResult
from caveviewer.core.diagnostics.logging import get_logger


_LOG = get_logger("ModelDiscovery")
OBJ_MATERIAL_SCAN_LIMIT_BYTES = 1024 * 1024


class SourceFormatId(str, Enum):
    """Stable identifiers for source formats CaveViewer can import."""

    OBJ = "obj"
    GLB = "glb"


@dataclass(frozen=True, slots=True)
class SourceFormat:
    """Release-policy metadata for one supported source-model format.

    The registry below is intentionally the single declaration of formats that
    discovery, presentation, and package metadata may advertise. Parser
    dispatch remains separate because it owns the format-specific work.
    """

    id: SourceFormatId
    extension: str
    display_name: str
    mime_type: str
    companion_file_extension: str | None = None

    @property
    def descriptor_path_key(self) -> str:
        """Return the descriptor key carrying this format's source path."""
        return f"{self.id.value}_path"

    @property
    def help_label(self) -> str:
        """Return concise guidance for selecting this format."""
        if self.companion_file_extension:
            return (
                f"{self.extension} (with a matching "
                f"{self.companion_file_extension})"
            )
        return self.extension


@dataclass(frozen=True, slots=True)
class SourceModelCandidate:
    """One source file found through the supported-format registry."""

    source_format: SourceFormat
    path: str


OBJ_SOURCE_FORMAT = SourceFormat(
    id=SourceFormatId.OBJ,
    extension=".obj",
    display_name="OBJ",
    mime_type="model/obj",
    companion_file_extension=".mtl",
)
GLB_SOURCE_FORMAT = SourceFormat(
    id=SourceFormatId.GLB,
    extension=".glb",
    display_name="GLB",
    mime_type="model/gltf-binary",
)
SUPPORTED_SOURCE_FORMATS = (OBJ_SOURCE_FORMAT, GLB_SOURCE_FORMAT)


def supported_source_formats() -> tuple[SourceFormat, ...]:
    """Return the immutable release-policy registry of supported formats."""
    return SUPPORTED_SOURCE_FORMATS


def source_format_for_id(
    format_id: SourceFormatId | str | None,
) -> SourceFormat | None:
    """Return one registered format for a descriptor identifier, if any."""
    if isinstance(format_id, SourceFormatId):
        normalized_id = format_id.value
    elif isinstance(format_id, str):
        normalized_id = format_id.strip().lower()
    else:
        return None
    return next(
        (
            source_format
            for source_format in SUPPORTED_SOURCE_FORMATS
            if source_format.id.value == normalized_id
        ),
        None,
    )


def source_format_for_path(path: str | os.PathLike[str]) -> SourceFormat | None:
    """Return the registered format selected by a source filename extension."""
    extension = os.path.splitext(os.fspath(path))[1].lower()
    return next(
        (
            source_format
            for source_format in SUPPORTED_SOURCE_FORMATS
            if source_format.extension == extension
        ),
        None,
    )


def probe_source_format(
    path: str | os.PathLike[str],
) -> CapabilityResult[SourceFormat]:
    """Report whether one selected source filename has a released importer.

    This is a pure classification of the selected path. It does not claim the
    file exists or that required companion assets are present; discovery keeps
    those filesystem checks at its boundary.
    """
    extension = os.path.splitext(os.fspath(path))[1].lower()
    source_format = source_format_for_path(path)
    if source_format is not None:
        return CapabilityResult.available(
            source_format,
            reason_code="map_source_format_available",
            evidence={
                "extension": extension,
                "format": source_format.id.value,
            },
        )
    return CapabilityResult.unavailable(
        reason_code="map_source_format_unsupported",
        evidence={"extension": extension or None},
    )


def probe_model_descriptor(
    model_descriptor: Mapping[str, Any],
) -> CapabilityResult[SourceFormat]:
    """Report whether a model descriptor selects a released source format."""
    raw_format = model_descriptor.get("format")
    source_format = source_format_for_id(raw_format)
    if source_format is not None:
        return CapabilityResult.available(
            source_format,
            reason_code="map_source_format_available",
            evidence={"format": source_format.id.value},
        )
    return CapabilityResult.unavailable(
        reason_code="map_source_format_unsupported",
        evidence={"format": str(raw_format) if raw_format is not None else None},
    )


def supported_source_format_summary(*, conjunction: str = "and") -> str:
    """Return a human-readable summary derived from the format registry."""
    labels = [source_format.help_label for source_format in SUPPORTED_SOURCE_FORMATS]
    if not labels:
        return "no source formats"
    if len(labels) == 1:
        return labels[0]
    if len(labels) == 2:
        return f"{labels[0]} {conjunction} {labels[1]}"
    return f"{', '.join(labels[:-1])}, {conjunction} {labels[-1]}"


def find_supported_source_files(folder: str) -> tuple[SourceModelCandidate, ...]:
    """Return source files found through the canonical supported-format list."""
    return tuple(
        SourceModelCandidate(source_format=source_format, path=path)
        for source_format in SUPPORTED_SOURCE_FORMATS
        for path in _source_files_for_format(folder, source_format)
    )


def _source_files_for_format(folder: str, source_format: SourceFormat) -> list[str]:
    return glob.glob(os.path.join(folder, f"*{source_format.extension}"))


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
    obj_candidates = _source_files_for_format(folder, OBJ_SOURCE_FORMAT)
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

    mtl_path = find_declared_material_file_for_obj(obj_path)
    if mtl_path:
        return mtl_path

    mtl_candidates = glob.glob(os.path.join(folder, "*.mtl"))
    if not mtl_candidates:
        raise FileNotFoundError(
            f"Found {os.path.basename(obj_path)} but no matching .mtl file in:\n  {folder}"
        )
    return mtl_candidates[0]


def find_declared_material_file_for_obj(
    obj_path: str,
    *,
    max_scan_bytes: int = OBJ_MATERIAL_SCAN_LIMIT_BYTES,
) -> str | None:
    """
    Return the existing material file declared by an OBJ ``mtllib`` header.

    OBJ files can be very large, so discovery reads only a bounded prefix where
    material-library declarations are expected instead of iterating the whole
    mesh payload.
    """
    if max_scan_bytes <= 0:
        return None

    folder = os.path.dirname(os.path.abspath(obj_path))
    with open(obj_path, "rb") as file_obj:
        header = file_obj.read(max_scan_bytes)

    lines = header.splitlines()
    if (
        header
        and len(header) >= max_scan_bytes
        and not header.endswith((b"\n", b"\r"))
    ):
        lines = lines[:-1]

    for raw_line in lines:
        line = raw_line.decode("utf-8", errors="replace").lstrip("\ufeff \t")
        parts = line.split(maxsplit=1)
        if parts and parts[0] == "mtllib":
            if len(parts) < 2:
                return None
            mtl_name = parts[1].strip()
            if not mtl_name:
                return None
            mtl_path = _resolve_companion_path(folder, mtl_name, "OBJ material")
            if os.path.exists(mtl_path):
                return mtl_path
            return None
    return None


def find_model_file(folder: str, *, logger: Any | None = None) -> dict:
    """
    Detect a supported model input and return a descriptor dictionary.

    Returns one of:

    - ``{"format": "obj", "obj_path": ..., "mtl_path": ...}``
    - ``{"format": "glb", "glb_path": ...}``
    """
    selected_path = os.path.abspath(folder)
    if os.path.isfile(selected_path):
        source_format = source_format_for_path(selected_path)
        if source_format is not None:
            return _model_descriptor_for_path(selected_path, source_format)
        raise FileNotFoundError(
            f"No supported model file found at:\n  {selected_path}\n\n"
            f"CaveViewer supports {supported_source_format_summary()} files."
        )

    folder = selected_path
    for source_format in SUPPORTED_SOURCE_FORMATS:
        candidates = _source_files_for_format(folder, source_format)
        if not candidates:
            continue
        if len(candidates) > 1:
            _info(
                logger,
                "Note: multiple "
                f"{source_format.extension} files found, using the first one: "
                f"{candidates[0]}",
            )
        model_path = candidates[0]
        return _model_descriptor_for_path(model_path, source_format)

    raise FileNotFoundError(
        f"No supported model file found in:\n  {folder}\n\n"
        f"CaveViewer supports {supported_source_format_summary()} files. "
        f"Make sure you selected the folder containing your exported map."
    )


def _model_descriptor_for_path(
    model_path: str,
    source_format: SourceFormat,
) -> dict:
    """Build one parser descriptor from a released source format and path."""
    descriptor = {
        "format": source_format.id.value,
        source_format.descriptor_path_key: model_path,
    }
    if source_format.id is SourceFormatId.OBJ:
        descriptor["mtl_path"] = find_material_file_for_obj(model_path)
    return descriptor
