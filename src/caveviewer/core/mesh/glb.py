"""
caveviewer.core.mesh.glb

Parses GLB (binary glTF) files into the same RawMesh shape
caveviewer.core.mesh.obj produces for OBJ -- see that module's RawMesh
docstring for the exact field contract this must satisfy.

Built on the `pygltflib` library (PyPI: pygltflib) rather than hand-
writing a glTF/GLB parser. glTF is a structured, precisely-specified
format (JSON scene description + packed binary buffers for vertex/index
data, optionally with embedded images), and pygltflib already handles
the format's full structure correctly, including the buffer-view/
accessor indirection glTF uses to describe how raw bytes map to typed
arrays.

Key structural differences from OBJ that this parser has to bridge:
  - glTF organizes geometry into "meshes" containing "primitives" (each
    primitive is one draw call's worth of geometry + one material) --
    roughly analogous to OBJ's usemtl-delimited material ranges, but
    accessed very differently (each primitive has its own accessor
    indices into the shared binary buffer, rather than OBJ's flat
    contiguous-face-range convention). This parser concatenates all
    primitives from all meshes in the scene into one flat RawMesh, with
    one MaterialRange per primitive -- which is exactly the same shape
    mesh.obj already produces per usemtl block, just sourced
    differently.
  - glTF positions/normals/UVs are NOT separately indexed per attribute
    the way OBJ's v/vt/vn can be -- a glTF primitive's single index
    buffer addresses one shared set of "vertices" where each vertex
    already has its position+normal+UV bundled together. This is
    actually a SIMPLER shape than OBJ's, and converts cleanly: this
    parser treats each primitive's own vertex range as if it were
    OBJ-style "one shared index per attribute," which is correct since
    that's exactly what a glTF vertex already is.
  - Textures are commonly EMBEDDED inside the .glb file's binary blob
    rather than referenced as separate files on disk. This parser
    extracts embedded image bytes directly (see _extract_embedded_images)
    and returns them as raw bytes rather than filenames. Core import turns
    those bytes into ordinary cache texture assets, and the render-layer
    texture manager consumes them through the same material mapping as OBJ
    textures.

The focused test suite covers this path with a generated minimal binary GLB
and direct validation failures for malformed accessor ranges.
"""

from __future__ import annotations

import os
import struct
from typing import Optional

import numpy as np

from caveviewer.core.mesh.obj import RawMesh, MaterialRange


# glTF accessor component type codes -> numpy dtype, per the glTF 2.0 spec
# (these are fixed, standardized integer codes, not something that varies
# by exporter -- this mapping is safe to hard-code).
_COMPONENT_TYPE_TO_DTYPE = {
    5120: np.int8,
    5121: np.uint8,
    5122: np.int16,
    5123: np.uint16,
    5125: np.uint32,
    5126: np.float32,
}

_TYPE_TO_NUM_COMPONENTS = {
    "SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4,
    "MAT2": 4, "MAT3": 9, "MAT4": 16,
}
_GLTF_TRIANGLES_MODE = 4
_GLTF_INDEX_COMPONENT_TYPES = {5121, 5123, 5125}
_RAW_MESH_MAX_INDEX = np.iinfo(np.int32).max


def _require_index(items, index: int, description: str):
    try:
        resolved_index = int(index)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid {description} index: {index!r}") from exc
    if resolved_index < 0 or resolved_index >= len(items or ()):
        raise ValueError(f"{description} index out of range: {resolved_index}")
    return items[resolved_index]


def _required_nonnegative_int(value, description: str) -> int:
    try:
        resolved = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid {description}: {value!r}") from exc
    if resolved < 0:
        raise ValueError(f"Invalid negative {description}: {resolved}")
    return resolved


def _optional_nonnegative_int(value, description: str) -> int:
    if value is None:
        return 0
    return _required_nonnegative_int(value, description)


def _accessor(gltf, accessor_index: int, description: str):
    return _require_index(gltf.accessors or (), accessor_index, description)


def _buffer_view(gltf, buffer_view_index: int, description: str):
    return _require_index(gltf.bufferViews or (), buffer_view_index, description)


def _buffer(gltf, buffer_index: int, description: str):
    return _require_index(gltf.buffers or (), buffer_index, description)


def _accessor_count(gltf, accessor_index: int, description: str) -> int:
    accessor = _accessor(gltf, accessor_index, description)
    return _required_nonnegative_int(accessor.count, f"{description} accessor count")


def _validate_primitive_mode(primitive, description: str) -> None:
    mode = getattr(primitive, "mode", None)
    if mode is not None and int(mode) != _GLTF_TRIANGLES_MODE:
        raise ValueError(
            f"{description} uses unsupported primitive mode {mode!r}; "
            "only triangle-list GLB meshes are supported"
        )


def _validate_accessor_layout(
    gltf,
    accessor_index: int,
    description: str,
    get_buffer_bytes,
):
    accessor = _accessor(gltf, accessor_index, description)
    if accessor.bufferView is None:
        raise ValueError(f"{description} accessor is missing bufferView")
    buffer_view = _buffer_view(gltf, accessor.bufferView, f"{description} bufferView")
    _buffer(gltf, buffer_view.buffer, f"{description} buffer")
    raw_bytes = get_buffer_bytes(buffer_view.buffer)
    if raw_bytes is None:
        raise ValueError(f"{description} buffer has no binary payload")

    try:
        dtype = _COMPONENT_TYPE_TO_DTYPE[accessor.componentType]
    except KeyError as exc:
        raise ValueError(
            f"{description} uses unsupported componentType {accessor.componentType!r}"
        ) from exc
    try:
        n_components = _TYPE_TO_NUM_COMPONENTS[accessor.type]
    except KeyError as exc:
        raise ValueError(
            f"{description} uses unsupported accessor type {accessor.type!r}"
        ) from exc

    count = _required_nonnegative_int(accessor.count, f"{description} accessor count")
    buffer_offset = _optional_nonnegative_int(
        buffer_view.byteOffset,
        f"{description} bufferView byteOffset",
    )
    accessor_offset = _optional_nonnegative_int(
        accessor.byteOffset,
        f"{description} accessor byteOffset",
    )
    view_length = _required_nonnegative_int(
        buffer_view.byteLength,
        f"{description} bufferView byteLength",
    )
    element_size = n_components * np.dtype(dtype).itemsize
    stride = buffer_view.byteStride
    if stride is not None:
        stride = _required_nonnegative_int(stride, f"{description} byteStride")
        if stride < element_size:
            raise ValueError(
                f"{description} byteStride {stride} is smaller than "
                f"element size {element_size}"
            )
        accessor_byte_count = 0 if count == 0 else (count - 1) * stride + element_size
    else:
        accessor_byte_count = count * element_size

    if buffer_offset + view_length > len(raw_bytes):
        raise ValueError(
            f"{description} bufferView range exceeds buffer payload "
            f"({buffer_offset}+{view_length}>{len(raw_bytes)})"
        )
    if accessor_offset + accessor_byte_count > view_length:
        raise ValueError(
            f"{description} accessor range exceeds bufferView "
            f"({accessor_offset}+{accessor_byte_count}>{view_length})"
        )

    return (
        raw_bytes,
        dtype,
        n_components,
        count,
        buffer_offset + accessor_offset,
        stride,
        element_size,
    )


def _validated_index_array(
    indices: np.ndarray,
    *,
    vertex_count: int,
    vertex_offset: int,
    description: str,
) -> np.ndarray:
    """Validate glTF primitive indices before storing them in RawMesh int32 arrays."""
    flat = np.asarray(indices).reshape(-1)
    if flat.size == 0:
        return flat.astype(np.int32, copy=False)

    min_index = int(flat.min())
    max_index = int(flat.max())
    if min_index < 0 or max_index >= vertex_count:
        raise ValueError(
            f"{description} indices reference vertex range "
            f"{min_index}..{max_index}, outside POSITION count {vertex_count}"
        )
    if vertex_offset + max_index > _RAW_MESH_MAX_INDEX:
        raise ValueError(
            f"{description} indices exceed RawMesh int32 index capacity"
        )
    return flat.astype(np.int32, copy=False)


def parse_glb(
    glb_path: str,
    progress_cb=None,
    preflight_cb=None,
) -> tuple[RawMesh, dict]:
    """
    Parses a GLB file into a RawMesh, plus {material_name: embedded_image_bytes}
    for any materials whose texture was embedded directly inside the file
    (the common case for GLB specifically, as opposed to .gltf+.bin+loose
    images, though this parser handles either since pygltflib abstracts
    over both).

    ``preflight_cb(vertex_count, uv_count, normal_count, face_count)``, if
    given, is called after the GLB structure is loaded and validated enough to
    estimate expanded mesh arrays, but before those arrays are materialized.

    Returns (mesh, material_to_embedded_bytes) -- the second dict is
    merged into the material-to-texture mapping core.map.importer builds,
    alongside any materials that instead reference an external image file
    by relative path (handled the same way an OBJ's .mtl reference would
    be, since pygltflib resolves those to plain filenames too).
    """
    from pygltflib import GLTF2

    if progress_cb:
        progress_cb("reading GLB file", 0.0)

    gltf = GLTF2().load(glb_path)

    if progress_cb:
        progress_cb("reading GLB file", 0.2)

    # GLB embeds its binary buffer data directly in the file; pygltflib
    # exposes this via get_data_from_buffer_uri / binary_blob() depending
    # on version, but the stable, documented way to get a buffer's raw
    # bytes for any glTF (embedded or external) is via this helper.
    def get_buffer_bytes(buffer_index: int) -> bytes:
        buffer = _buffer(gltf, buffer_index, "buffer")
        payload = (
            gltf.get_data_from_buffer_uri(buffer.uri)
            if buffer.uri
            else gltf.binary_blob()
        )
        if payload is None:
            raise ValueError(f"GLB buffer {buffer_index} has no binary payload")
        return payload

    def read_accessor(accessor_index: int) -> np.ndarray:
        """Resolves one glTF accessor into a numpy array, following the
        accessor -> bufferView -> buffer indirection the format uses to
        describe how raw bytes map to typed values."""
        (
            raw_bytes,
            dtype,
            n_components,
            count,
            start,
            stride,
            element_size,
        ) = _validate_accessor_layout(
            gltf,
            accessor_index,
            f"accessor {accessor_index}",
            get_buffer_bytes,
        )

        if stride and stride != element_size:
            values = np.empty((count, n_components), dtype=dtype)
            for i in range(count):
                offset = start + i * stride
                values[i] = np.frombuffer(
                    raw_bytes,
                    dtype=dtype,
                    count=n_components,
                    offset=offset,
                )
            return values

        flat = np.frombuffer(
            raw_bytes,
            dtype=dtype,
            count=count * n_components,
            offset=start,
        )
        return flat.reshape((count, n_components)) if n_components > 1 else flat

    if progress_cb:
        progress_cb("reading mesh primitives", 0.35)

    all_positions = []
    all_uvs = []
    all_normals = []
    all_face_idx = []
    material_ranges = []

    vertex_offset = 0
    face_offset = 0

    primitive_infos = []
    total_vertices = 0
    total_uvs = 0
    total_normals = 0
    total_faces = 0

    for mesh_idx, gltf_mesh in enumerate(gltf.meshes or []):
        for prim_idx, primitive in enumerate(gltf_mesh.primitives or []):
            description = f"mesh {mesh_idx} primitive {prim_idx}"
            _validate_primitive_mode(primitive, description)
            attributes = getattr(primitive, "attributes", None)
            pos_accessor_idx = getattr(attributes, "POSITION", None)
            if pos_accessor_idx is None:
                continue  # a primitive with no positions is degenerate; skip it
            vertex_count = _accessor_count(
                gltf,
                pos_accessor_idx,
                f"{description} POSITION",
            )
            uv_accessor_idx = getattr(attributes, "TEXCOORD_0", None)
            if uv_accessor_idx is not None:
                uv_count = _accessor_count(
                    gltf,
                    uv_accessor_idx,
                    f"{description} TEXCOORD_0",
                )
                if uv_count != vertex_count:
                    raise ValueError(
                        f"{description} TEXCOORD_0 count {uv_count} does not "
                        f"match POSITION count {vertex_count}"
                    )
            normal_accessor_idx = getattr(attributes, "NORMAL", None)
            if normal_accessor_idx is not None:
                normal_count = _accessor_count(
                    gltf,
                    normal_accessor_idx,
                    f"{description} NORMAL",
                )
                if normal_count != vertex_count:
                    raise ValueError(
                        f"{description} NORMAL count {normal_count} does not "
                        f"match POSITION count {vertex_count}"
                    )
            if primitive.indices is not None:
                index_accessor = _accessor(
                    gltf,
                    primitive.indices,
                    f"{description} indices",
                )
                if index_accessor.componentType not in _GLTF_INDEX_COMPONENT_TYPES:
                    raise ValueError(
                        f"{description} indices use unsupported componentType "
                        f"{index_accessor.componentType!r}"
                    )
                index_count = _required_nonnegative_int(
                    index_accessor.count,
                    f"{description} index count",
                )
                if index_count % 3 != 0:
                    raise ValueError(
                        f"{description} index count {index_count} is not "
                        "triangle-aligned"
                    )
                face_count = index_count // 3
            else:
                if vertex_count % 3 != 0:
                    raise ValueError(
                        f"{description} POSITION count {vertex_count} is not "
                        "triangle-aligned for an unindexed triangle list"
                    )
                face_count = vertex_count // 3
            primitive_infos.append(
                (
                    mesh_idx,
                    prim_idx,
                    primitive,
                    pos_accessor_idx,
                    uv_accessor_idx,
                    normal_accessor_idx,
                    vertex_count,
                    face_count,
                )
            )
            total_vertices += vertex_count
            total_uvs += vertex_count
            total_normals += vertex_count
            total_faces += face_count

    if preflight_cb:
        preflight_cb(total_vertices, total_uvs, total_normals, total_faces)
    if total_vertices > _RAW_MESH_MAX_INDEX:
        raise ValueError("GLB vertex count exceeds RawMesh int32 index capacity")

    # Walk every mesh's every primitive, in order -- this fixed,
    # deterministic order is what makes "one MaterialRange per primitive,
    # in the order encountered" a correct, stable mapping.
    for (
        mesh_idx,
        prim_idx,
        primitive,
        pos_accessor_idx,
        uv_accessor_idx,
        normal_accessor_idx,
        _vertex_count,
        n_tris_this_prim,
    ) in primitive_infos:
        positions = read_accessor(pos_accessor_idx).astype(np.float32)
        n_verts_this_prim = positions.shape[0]
        all_positions.append(positions)

        if uv_accessor_idx is not None:
            uvs = read_accessor(uv_accessor_idx).astype(np.float32)
        else:
            uvs = np.zeros((n_verts_this_prim, 2), dtype=np.float32)
        all_uvs.append(uvs)

        if normal_accessor_idx is not None:
            normals = read_accessor(normal_accessor_idx).astype(np.float32)
        else:
            normals = np.zeros((n_verts_this_prim, 3), dtype=np.float32)
        all_normals.append(normals)

        if primitive.indices is not None:
            raw_indices = read_accessor(primitive.indices)
            indices = _validated_index_array(
                raw_indices,
                vertex_count=n_verts_this_prim,
                vertex_offset=vertex_offset,
                description=f"mesh {mesh_idx} primitive {prim_idx}",
            )
        else:
            # no index buffer means the vertex stream is already in
            # draw order, implicitly 0,1,2,3,4,5...
            indices = np.arange(n_verts_this_prim, dtype=np.int32)

        # glTF primitives are required to be triangle lists by
        # default (mode 4, TRIANGLES) -- the overwhelmingly common
        # case for any exported scan -- so no fan-triangulation
        # needed here the way OBJ/PLY can require; just reshape the
        # flat index stream into (N, 3) triangles directly.
        tris = (
            indices[: n_tris_this_prim * 3].reshape((n_tris_this_prim, 3))
            + vertex_offset
        )
        all_face_idx.append(tris)

        material_name = (
            f"gltf_material_{primitive.material}"
            if primitive.material is not None
            else f"gltf_mesh{mesh_idx}_prim{prim_idx}_untextured"
        )
        material_ranges.append(MaterialRange(
            material_name=material_name,
            start_face=face_offset,
            end_face=face_offset + n_tris_this_prim,
        ))

        vertex_offset += n_verts_this_prim
        face_offset += n_tris_this_prim

    if progress_cb:
        progress_cb("assembling mesh", 0.7)

    positions = np.concatenate(all_positions, axis=0) if all_positions else np.zeros((0, 3), dtype=np.float32)
    uvs = np.concatenate(all_uvs, axis=0) if all_uvs else np.zeros((0, 2), dtype=np.float32)
    normals = np.concatenate(all_normals, axis=0) if all_normals else np.zeros((0, 3), dtype=np.float32)
    face_pos_idx = np.concatenate(all_face_idx, axis=0) if all_face_idx else np.zeros((0, 3), dtype=np.int32)

    # glTF vertices already bundle position+UV+normal together (one
    # shared index per vertex, unlike OBJ's separate v/vt/vn index
    # streams) -- so the UV/normal index for any triangle corner is
    # simply the same index used for its position, since they're already
    # the same array length and correspondence.
    face_uv_idx = face_pos_idx.copy()
    face_nrm_idx = face_pos_idx.copy()

    mesh = RawMesh(
        positions=positions,
        uvs=uvs,
        normals=normals,
        face_pos_idx=face_pos_idx,
        face_uv_idx=face_uv_idx,
        face_nrm_idx=face_nrm_idx,
        material_ranges=material_ranges,
        mtl_file=None,
    )

    if progress_cb:
        progress_cb("extracting embedded textures", 0.9)

    material_to_embedded_bytes = _extract_embedded_images(gltf, get_buffer_bytes)

    if progress_cb:
        progress_cb("done", 1.0)

    return mesh, material_to_embedded_bytes


def _extract_embedded_images(gltf, get_buffer_bytes) -> dict:
    """
    Maps each material (by the same synthetic "gltf_material_<index>"
    name used above) to its raw embedded image bytes, for any material
    whose texture's image data lives inside a bufferView (the standard
    way GLB embeds images) rather than as an external file URI.

    Materials whose texture instead references an external file (a plain
    relative-path URI, common for .gltf+.bin+loose-images bundles rather
    than single-file .glb) are intentionally NOT included here -- the
    caller falls back to treating that case as an ordinary filename, the
    same as OBJ/.mtl's existing convention, since the render-layer texture
    manager reads plain files from textures_dir.
    """
    result = {}

    for material_idx, material in enumerate(gltf.materials or []):
        pbr = getattr(material, "pbrMetallicRoughness", None)
        if pbr is None or pbr.baseColorTexture is None:
            continue

        texture_idx = pbr.baseColorTexture.index
        texture = _require_index(
            gltf.textures or (),
            texture_idx,
            f"material {material_idx} texture",
        )
        if texture.source is None:
            continue

        image = _require_index(
            gltf.images or (),
            texture.source,
            f"material {material_idx} image",
        )

        if image.bufferView is not None:
            # embedded: the image's raw encoded bytes (JPEG/PNG file
            # bytes, exactly as if you'd opened the .jpg/.png on disk)
            # live directly in a bufferView, no separate accessor
            # indirection needed for images specifically (unlike vertex
            # data) -- bufferViews for images just point at a contiguous
            # byte range holding the already-encoded image file.
            buffer_view = _buffer_view(
                gltf,
                image.bufferView,
                f"material {material_idx} image bufferView",
            )
            raw_bytes = get_buffer_bytes(buffer_view.buffer)
            start = _optional_nonnegative_int(
                buffer_view.byteOffset,
                f"material {material_idx} image bufferView byteOffset",
            )
            length = _required_nonnegative_int(
                buffer_view.byteLength,
                f"material {material_idx} image bufferView byteLength",
            )
            if start + length > len(raw_bytes):
                raise ValueError(
                    f"material {material_idx} image bufferView range exceeds "
                    f"buffer payload ({start}+{length}>{len(raw_bytes)})"
                )
            image_bytes = raw_bytes[start:start + length]
            result[f"gltf_material_{material_idx}"] = image_bytes
        # else: image.uri points at an external file -- left for the
        # caller to handle as a plain filename, same as OBJ's convention.

    return result
