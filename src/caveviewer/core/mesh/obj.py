"""
caveviewer.core.mesh.obj

Streaming parser for large Wavefront OBJ files produced by Agisoft Metashape.

Design constraints driving this implementation:
  - Source files can be 500MB-2GB+ with 5-20M triangles.
  - We must NOT load the file into a single Python list-of-tuples structure;
    Python object overhead would multiply memory use 10-20x.
  - We do a single streaming pass, writing directly into pre-sized numpy
    arrays. Since OBJ doesn't tell us face count up front, we do a fast
    line-count pre-pass (just counting 'f ' / 'v ' prefixes) to size arrays,
    then a second pass to actually fill them. Two passes over text is much
    cheaper than dynamic Python list growth + final conversion.

Agisoft OBJ exports are well-behaved: vertices are 'v x y z', texture coords
are 'vt u v', normals are 'vn x y z' (sometimes omitted), and faces are
'f v1/vt1/vn1 v2/vt2/vn2 v3/vt3/vn3' (always triangulated on export, but we
defensively fan-triangulate anything with >3 verts). Faces reference a
'usemtl <name>' that switches the active material/texture tile.
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass, field

import numpy as np

_FACE_VERT_RE = re.compile(r"(-?\d+)(?:/(-?\d*)(?:/(-?\d*))?)?")
OBJ_SCAN_THROTTLE_ENV_VAR = "CAVEVIEWER_OBJ_SCAN_THROTTLE_MS"
_SCAN_PROGRESS_MIN_INTERVAL_SECONDS = 0.20
_SCAN_THROTTLE_INTERVAL_BYTES = 8 * 1024 * 1024
_SCAN_PROGRESS_WEIGHT = 0.20


def _iter_text_lines(path: str, *, kind: str, buffering: int = 1024 * 1024):
    try:
        with open(
            path,
            "r",
            buffering=buffering,
            encoding="utf-8",
            errors="strict",
        ) as fh:
            for line_number, line in enumerate(fh, start=1):
                yield line_number, line
    except UnicodeDecodeError as exc:
        raise ValueError(
            f"{kind} file {path} is not valid UTF-8 near byte {exc.start}"
        ) from exc


def _line_error(kind: str, path: str, line_number: int, message: str) -> ValueError:
    return ValueError(f"Malformed {kind} file {path}:{line_number}: {message}")


def _is_directive(line: str, directive: str) -> bool:
    return line == directive or line.startswith(directive + " ")


def _obj_line_error(path: str, line_number: int, message: str) -> ValueError:
    return _line_error("OBJ", path, line_number, message)


def _mtl_line_error(path: str, line_number: int, message: str) -> ValueError:
    return _line_error("MTL", path, line_number, message)


def _directive_argument(
    line: str,
    directive: str,
    *,
    kind: str,
    path: str,
    line_number: int,
) -> str:
    parts = line.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        raise _line_error(
            kind,
            path,
            line_number,
            f"{directive} requires an argument",
        )
    return parts[1].strip()


def _parse_float_fields(
    parts: list[str],
    *,
    expected: int,
    directive: str,
    path: str,
    line_number: int,
) -> tuple[float, ...]:
    if len(parts) < expected + 1:
        raise _obj_line_error(
            path,
            line_number,
            f"{directive} requires at least {expected} numeric value(s)",
        )
    try:
        return tuple(float(parts[index]) for index in range(1, expected + 1))
    except ValueError as exc:
        raise _obj_line_error(
            path,
            line_number,
            f"{directive} contains a non-numeric value",
        ) from exc


def _obj_scan_throttle_seconds() -> float:
    raw = os.getenv(OBJ_SCAN_THROTTLE_ENV_VAR, "").strip()
    if raw:
        try:
            return max(0.0, min(50.0, float(raw))) / 1000.0
        except ValueError:
            pass
    return 0.001 if os.name == "nt" else 0.0


@dataclass
class MaterialRange:
    """A contiguous run of faces in the global face array that use one material."""
    material_name: str
    start_face: int   # inclusive
    end_face: int     # exclusive


@dataclass
class RawMesh:
    """
    Result of parsing one OBJ. Index arrays are 0-based into `positions` /
    `uvs` / `normals`, already resolved (OBJ's own indices are 1-based and
    support negative/relative indexing; we normalize both away here so
    nothing downstream has to think about OBJ quirks again).
    """
    positions: np.ndarray            # (Nv, 3) float32
    uvs: np.ndarray                  # (Nvt, 2) float32, may be zero-length
    normals: np.ndarray              # (Nvn, 3) float32, may be zero-length

    face_pos_idx: np.ndarray         # (Nf, 3) int32 -> positions
    face_uv_idx: np.ndarray          # (Nf, 3) int32 -> uvs (or -1 if none)
    face_nrm_idx: np.ndarray         # (Nf, 3) int32 -> normals (or -1 if none)

    material_ranges: list[MaterialRange] = field(default_factory=list)
    mtl_file: str | None = None


@dataclass
class ObjVertexData:
    """OBJ vertex attributes loaded without retaining any face index arrays."""

    positions: np.ndarray
    uvs: np.ndarray
    normals: np.ndarray
    face_count: int
    mtl_file: str | None = None


@dataclass
class ObjFaceBatch:
    """A bounded batch of triangulated OBJ faces and their active materials."""

    face_pos_idx: np.ndarray
    face_uv_idx: np.ndarray
    face_nrm_idx: np.ndarray
    material_names: list[str | None]


def _count_prepass(obj_path: str, progress_cb=None) -> tuple[int, int, int, int]:
    """
    Fast first pass: count vertices/uvs/normals/faces (post-triangulation
    estimate) so we can pre-allocate numpy arrays of the right size instead
    of growing them dynamically. Triangulation count assumes worst case of
    triangle-or-quad faces (Agisoft only ever emits these), counting a quad
    as 2 triangles.
    """
    n_v = n_vt = n_vn = n_f = 0
    file_size = os.path.getsize(obj_path)
    bytes_read = 0
    last_reported_fraction = -1.0
    last_report_time = 0.0
    next_throttle_at = _SCAN_THROTTLE_INTERVAL_BYTES
    throttle_seconds = _obj_scan_throttle_seconds()

    for line_number, line in _iter_text_lines(obj_path, kind="OBJ"):
        bytes_read += len(line)
        if progress_cb and file_size:
            now = time.perf_counter()
            fraction = max(0.0, min(1.0, bytes_read / file_size))
            if (
                fraction >= 1.0
                or fraction - last_reported_fraction >= 0.01
                or now - last_report_time >= _SCAN_PROGRESS_MIN_INTERVAL_SECONDS
            ):
                progress_cb("scanning file", fraction)
                last_reported_fraction = fraction
                last_report_time = now

        if throttle_seconds > 0.0 and bytes_read >= next_throttle_at:
            time.sleep(throttle_seconds)
            while next_throttle_at <= bytes_read:
                next_throttle_at += _SCAN_THROTTLE_INTERVAL_BYTES

        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if _is_directive(stripped, "v"):
            n_v += 1
        elif _is_directive(stripped, "vt"):
            n_vt += 1
        elif _is_directive(stripped, "vn"):
            n_vn += 1
        elif _is_directive(stripped, "f"):
            n_tokens = len(stripped.split()) - 1
            if n_tokens < 3:
                raise _obj_line_error(
                    obj_path,
                    line_number,
                    f"OBJ face expected at least 3 vertices, got {n_tokens}",
                )
            n_f += n_tokens - 2
    if progress_cb:
        progress_cb("scanning file", 1.0)
    return n_v, n_vt, n_vn, n_f


def _resolve_index(raw: int, count_so_far: int) -> int:
    """OBJ indices are 1-based; negative indices count back from the most
    recently defined element. Convert both to a normal 0-based index."""
    if raw > 0:
        resolved = raw - 1
    elif raw < 0:
        resolved = count_so_far + raw
    else:
        raise ValueError("OBJ index of 0 is invalid")
    if resolved < 0 or resolved >= count_so_far:
        raise ValueError(
            f"OBJ index {raw} is out of range for {count_so_far} parsed values"
        )
    return resolved


def _parse_face_vertices(
    tokens: list[str],
    *,
    vertex_count: int,
    uv_count: int,
    normal_count: int,
    path: str | None = None,
    line_number: int | None = None,
) -> list[tuple[int, int, int]]:
    def malformed(message: str) -> ValueError:
        if path is not None and line_number is not None:
            return _obj_line_error(path, line_number, message)
        return ValueError(message)

    if len(tokens) < 3:
        raise malformed(
            f"OBJ face expected at least 3 vertices, got {len(tokens)}"
        )

    verts = []
    for tok in tokens:
        if tok.endswith("/"):
            raise malformed(f"Malformed OBJ face token: {tok!r}")
        m = _FACE_VERT_RE.fullmatch(tok)
        if not m:
            raise malformed(f"Malformed OBJ face token: {tok!r}")
        try:
            p_raw = int(m.group(1))
            p_idx = _resolve_index(p_raw, vertex_count)
        except ValueError as exc:
            raise malformed(str(exc)) from exc
        uv_idx = -1
        nrm_idx = -1
        try:
            if m.group(2):
                uv_idx = _resolve_index(int(m.group(2)), uv_count)
            if m.group(3):
                nrm_idx = _resolve_index(int(m.group(3)), normal_count)
        except ValueError as exc:
            raise malformed(str(exc)) from exc
        verts.append((p_idx, uv_idx, nrm_idx))
    return verts


def parse_obj_vertices(obj_path: str, progress_cb=None, preflight_cb=None) -> ObjVertexData:
    """
    Parse only OBJ vertex/UV/normal arrays.

    This is the front half of the incremental importer. Faces are deliberately
    skipped here so large maps do not allocate whole-model face-index arrays.
    """
    if progress_cb:
        progress_cb("scanning file", 0.0)

    def scan_progress(stage: str, frac: float) -> None:
        if progress_cb:
            progress_cb(stage, _SCAN_PROGRESS_WEIGHT * max(0.0, min(1.0, frac)))

    n_v, n_vt, n_vn, n_f_est = _count_prepass(obj_path, progress_cb=scan_progress)

    if preflight_cb:
        preflight_cb(n_v, n_vt, n_vn, n_f_est)

    positions = np.empty((n_v, 3), dtype=np.float32)
    uvs = np.empty((n_vt, 2), dtype=np.float32)
    normals = np.empty((n_vn, 3), dtype=np.float32)

    vi = vti = vni = 0
    mtl_file = None
    file_size = os.path.getsize(obj_path)
    bytes_read = 0
    last_reported = 0.0

    for line_number, line in _iter_text_lines(obj_path, kind="OBJ"):
        bytes_read += len(line)
        if progress_cb and file_size:
            frac = bytes_read / file_size
            if frac - last_reported > 0.01:
                progress_cb(
                    "parsing vertices",
                    _SCAN_PROGRESS_WEIGHT + (1.0 - _SCAN_PROGRESS_WEIGHT) * frac,
                )
                last_reported = frac

        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue

        if _is_directive(stripped, "v"):
            parts = stripped.split()
            x, y, z = _parse_float_fields(
                parts,
                expected=3,
                directive="v",
                path=obj_path,
                line_number=line_number,
            )
            positions[vi, 0] = x
            positions[vi, 1] = y
            positions[vi, 2] = z
            vi += 1

        elif _is_directive(stripped, "vt"):
            parts = stripped.split()
            u, v = _parse_float_fields(
                parts,
                expected=2,
                directive="vt",
                path=obj_path,
                line_number=line_number,
            )
            uvs[vti, 0] = u
            uvs[vti, 1] = v
            vti += 1

        elif _is_directive(stripped, "vn"):
            parts = stripped.split()
            x, y, z = _parse_float_fields(
                parts,
                expected=3,
                directive="vn",
                path=obj_path,
                line_number=line_number,
            )
            normals[vni, 0] = x
            normals[vni, 1] = y
            normals[vni, 2] = z
            vni += 1

        elif _is_directive(stripped, "mtllib"):
            mtl_file = _directive_argument(
                stripped,
                "mtllib",
                kind="OBJ",
                path=obj_path,
                line_number=line_number,
            )

    if progress_cb:
        progress_cb("parsing vertices", 1.0)

    return ObjVertexData(
        positions=positions,
        uvs=uvs,
        normals=normals,
        face_count=n_f_est,
        mtl_file=mtl_file,
    )


def iter_obj_face_batches(
    obj_path: str,
    *,
    batch_size: int = 200_000,
    progress_cb=None,
):
    """Yield bounded batches of triangulated OBJ faces with material names."""
    batch_size = max(1, int(batch_size))
    face_pos_idx = np.empty((batch_size, 3), dtype=np.int32)
    face_uv_idx = np.empty((batch_size, 3), dtype=np.int32)
    face_nrm_idx = np.empty((batch_size, 3), dtype=np.int32)
    material_names: list[str | None] = [None] * batch_size

    file_size = os.path.getsize(obj_path)
    bytes_read = 0
    last_reported = 0.0
    vi = vti = vni = 0
    fi = 0
    current_material = None

    def emit_batch():
        nonlocal face_pos_idx, face_uv_idx, face_nrm_idx, material_names, fi
        if fi <= 0:
            return None
        batch = ObjFaceBatch(
            face_pos_idx=face_pos_idx[:fi],
            face_uv_idx=face_uv_idx[:fi],
            face_nrm_idx=face_nrm_idx[:fi],
            material_names=material_names[:fi],
        )
        face_pos_idx = np.empty((batch_size, 3), dtype=np.int32)
        face_uv_idx = np.empty((batch_size, 3), dtype=np.int32)
        face_nrm_idx = np.empty((batch_size, 3), dtype=np.int32)
        material_names = [None] * batch_size
        fi = 0
        return batch

    for line_number, line in _iter_text_lines(obj_path, kind="OBJ"):
        bytes_read += len(line)
        if progress_cb and file_size:
            frac = bytes_read / file_size
            if frac - last_reported > 0.01:
                progress_cb("bucketing faces", frac)
                last_reported = frac

        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue

        if _is_directive(stripped, "v"):
            vi += 1
        elif _is_directive(stripped, "vt"):
            vti += 1
        elif _is_directive(stripped, "vn"):
            vni += 1
        elif _is_directive(stripped, "usemtl"):
            current_material = _directive_argument(
                stripped,
                "usemtl",
                kind="OBJ",
                path=obj_path,
                line_number=line_number,
            )
        elif _is_directive(stripped, "f"):
            verts = _parse_face_vertices(
                stripped.split()[1:],
                vertex_count=vi,
                uv_count=vti,
                normal_count=vni,
                path=obj_path,
                line_number=line_number,
            )
            for k in range(1, len(verts) - 1):
                a, b, c = verts[0], verts[k], verts[k + 1]
                face_pos_idx[fi] = (a[0], b[0], c[0])
                face_uv_idx[fi] = (a[1], b[1], c[1])
                face_nrm_idx[fi] = (a[2], b[2], c[2])
                material_names[fi] = current_material
                fi += 1
                if fi >= batch_size:
                    batch = emit_batch()
                    if batch is not None:
                        yield batch

    batch = emit_batch()
    if batch is not None:
        yield batch
    if progress_cb:
        progress_cb("bucketing faces", 1.0)


def parse_obj(obj_path: str, progress_cb=None, preflight_cb=None) -> RawMesh:
    """
    Parse `obj_path` into a RawMesh.

    progress_cb(stage: str, fraction: float) is called periodically if given,
    so a GUI can show a progress bar during the (one-time, then cached)
    import of a large map.

    preflight_cb(vertex_count, uv_count, normal_count, face_count), if given,
    is called after the count pass and before the large NumPy arrays are
    allocated. Raising from this callback rejects an import before memory
    pressure can destabilize the desktop.
    """
    if progress_cb:
        progress_cb("scanning file", 0.0)

    def scan_progress(stage: str, frac: float) -> None:
        if progress_cb:
            progress_cb(stage, _SCAN_PROGRESS_WEIGHT * max(0.0, min(1.0, frac)))

    n_v, n_vt, n_vn, n_f_est = _count_prepass(obj_path, progress_cb=scan_progress)

    if preflight_cb:
        preflight_cb(n_v, n_vt, n_vn, n_f_est)

    positions = np.empty((n_v, 3), dtype=np.float32)
    uvs = np.empty((n_vt, 2), dtype=np.float32)
    normals = np.empty((n_vn, 3), dtype=np.float32)

    face_pos_idx = np.empty((n_f_est, 3), dtype=np.int32)
    face_uv_idx = np.full((n_f_est, 3), -1, dtype=np.int32)
    face_nrm_idx = np.full((n_f_est, 3), -1, dtype=np.int32)

    material_ranges: list[MaterialRange] = []
    mtl_file = None

    vi = vti = vni = fi = 0
    current_material = None
    current_material_start_face = 0

    file_size = os.path.getsize(obj_path)
    bytes_read = 0
    last_reported = 0.0

    for line_number, line in _iter_text_lines(obj_path, kind="OBJ"):
        bytes_read += len(line)
        if progress_cb and file_size:
            frac = bytes_read / file_size
            if frac - last_reported > 0.01:
                progress_cb(
                    "parsing geometry",
                    _SCAN_PROGRESS_WEIGHT + (1.0 - _SCAN_PROGRESS_WEIGHT) * frac,
                )
                last_reported = frac

        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue

        if _is_directive(stripped, "v"):
            parts = stripped.split()
            x, y, z = _parse_float_fields(
                parts,
                expected=3,
                directive="v",
                path=obj_path,
                line_number=line_number,
            )
            positions[vi, 0] = x
            positions[vi, 1] = y
            positions[vi, 2] = z
            vi += 1

        elif _is_directive(stripped, "vt"):
            parts = stripped.split()
            u, v = _parse_float_fields(
                parts,
                expected=2,
                directive="vt",
                path=obj_path,
                line_number=line_number,
            )
            uvs[vti, 0] = u
            uvs[vti, 1] = v
            vti += 1

        elif _is_directive(stripped, "vn"):
            parts = stripped.split()
            x, y, z = _parse_float_fields(
                parts,
                expected=3,
                directive="vn",
                path=obj_path,
                line_number=line_number,
            )
            normals[vni, 0] = x
            normals[vni, 1] = y
            normals[vni, 2] = z
            vni += 1

        elif _is_directive(stripped, "f"):
            verts = _parse_face_vertices(
                stripped.split()[1:],
                vertex_count=vi,
                uv_count=vti,
                normal_count=vni,
                path=obj_path,
                line_number=line_number,
            )

            # fan-triangulate (handles tris natively, n-gons defensively)
            for k in range(1, len(verts) - 1):
                a, b, c = verts[0], verts[k], verts[k + 1]
                face_pos_idx[fi] = (a[0], b[0], c[0])
                face_uv_idx[fi] = (a[1], b[1], c[1])
                face_nrm_idx[fi] = (a[2], b[2], c[2])
                fi += 1

        elif _is_directive(stripped, "usemtl"):
            name = _directive_argument(
                stripped,
                "usemtl",
                kind="OBJ",
                path=obj_path,
                line_number=line_number,
            )
            if current_material is not None and fi > current_material_start_face:
                material_ranges.append(MaterialRange(
                    current_material, current_material_start_face, fi))
            current_material = name
            current_material_start_face = fi

        elif _is_directive(stripped, "mtllib"):
            mtl_file = _directive_argument(
                stripped,
                "mtllib",
                kind="OBJ",
                path=obj_path,
                line_number=line_number,
            )

    if current_material is not None and fi > current_material_start_face:
        material_ranges.append(MaterialRange(
            current_material, current_material_start_face, fi))

    if progress_cb:
        progress_cb("done", 1.0)

    return RawMesh(
        positions=positions,
        uvs=uvs,
        normals=normals,
        face_pos_idx=face_pos_idx[:fi],
        face_uv_idx=face_uv_idx[:fi],
        face_nrm_idx=face_nrm_idx[:fi],
        material_ranges=material_ranges,
        mtl_file=mtl_file,
    )


@dataclass
class Material:
    name: str
    diffuse_texture: str | None  # filename relative to the mtl's folder


def parse_mtl(mtl_path: str) -> dict[str, Material]:
    """Parse a .mtl file into {material_name: Material}."""
    materials: dict[str, Material] = {}
    current_name = None
    current_tex = None

    for line_number, line in _iter_text_lines(mtl_path, kind="MTL"):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if _is_directive(line, "newmtl"):
            if current_name is not None:
                materials[current_name] = Material(current_name, current_tex)
            current_name = _directive_argument(
                line,
                "newmtl",
                kind="MTL",
                path=mtl_path,
                line_number=line_number,
            )
            current_tex = None
        elif _is_directive(line, "map_Kd"):
            if current_name is None:
                raise _mtl_line_error(
                    mtl_path,
                    line_number,
                    "map_Kd appears before newmtl",
                )
            # Support quoted and unquoted filenames. Strip enclosing double
            # quotes only if they are the first and last characters.
            # This handles names with spaces like: map_Kd "My Texture.jpg"
            path = _directive_argument(
                line,
                "map_Kd",
                kind="MTL",
                path=mtl_path,
                line_number=line_number,
            )
            if len(path) > 1 and path.startswith('"') and path.endswith('"'):
                current_tex = path[1:-1]
            else:
                current_tex = path

    if current_name is not None:
        materials[current_name] = Material(current_name, current_tex)

    return materials
