"""
core/obj_parser.py

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


def _count_prepass(obj_path: str) -> tuple[int, int, int, int]:
    """
    Fast first pass: count vertices/uvs/normals/faces (post-triangulation
    estimate) so we can pre-allocate numpy arrays of the right size instead
    of growing them dynamically. Triangulation count assumes worst case of
    triangle-or-quad faces (Agisoft only ever emits these), counting a quad
    as 2 triangles.
    """
    n_v = n_vt = n_vn = n_f = 0
    with open(obj_path, "r", buffering=1024 * 1024, errors="replace") as fh:
        for line in fh:
            if not line:
                continue
            prefix = line[:2]
            if prefix == "v ":
                n_v += 1
            elif prefix == "vt":
                n_vt += 1
            elif prefix == "vn":
                n_vn += 1
            elif prefix == "f ":
                n_tokens = len(line.split()) - 1
                if n_tokens >= 3:
                    n_f += n_tokens - 2
    return n_v, n_vt, n_vn, n_f


def _resolve_index(raw: int, count_so_far: int) -> int:
    """OBJ indices are 1-based; negative indices count back from the most
    recently defined element. Convert both to a normal 0-based index."""
    if raw > 0:
        return raw - 1
    if raw < 0:
        return count_so_far + raw
    raise ValueError("OBJ index of 0 is invalid")


def _parse_obj_stream(obj_path: str, progress_cb, timing: dict | None) -> RawMesh:
    """
    Chunked streaming parser for large OBJ files.

    Reads one line at a time so I/O buffer RAM stays constant regardless of
    file size.  Vertex floats and face indices are still converted in batches
    via np.fromstring for speed.  Peak extra memory: ~30 MB for in-flight
    batches; output arrays scale with triangle count (unavoidable).

    Assumes triangulated faces with v/vt/vn format (standard Agisoft export).
    A per-line fallback handles any batch that doesn't conform.
    """
    _V_BATCH = 200_000   # vertex lines flushed to numpy at a time
    _F_BATCH = 500_000   # face lines flushed to numpy at a time

    v_strs:  list[str] = []
    vt_strs: list[str] = []
    vn_strs: list[str] = []
    v_chunks:  list[np.ndarray] = []
    vt_chunks: list[np.ndarray] = []
    vn_chunks: list[np.ndarray] = []

    f_strs:    list[str] = []
    fp_chunks: list[np.ndarray] = []
    fu_chunks: list[np.ndarray] = []
    fn_chunks: list[np.ndarray] = []
    face_count = 0

    material_ranges: list[MaterialRange] = []
    current_mat:   str | None = None
    current_start: int        = 0
    mtl_file:      str | None = None

    file_size   = os.path.getsize(obj_path)
    bytes_read  = 0
    last_report = 0.0

    def _flush_verts(strs: list, chunks: list, cols: int) -> None:
        if strs:
            chunks.append(
                np.fromstring("".join(strs), dtype=np.float32, sep=" ").reshape(-1, cols)
            )
            strs.clear()

    def _flush_faces() -> None:
        nonlocal face_count
        if not f_strs:
            return
        text = "".join(line[2:].replace("/", " ") for line in f_strs)
        raw  = np.fromstring(text, dtype=np.int32, sep=" ")
        n    = len(f_strs)
        if n > 0 and raw.size == n * 9 and int(raw.min()) >= 1:
            # Fast path: triangles with pos/uv/nrm, positive indices only.
            raw = raw.reshape(n, 9) - 1
            fp_chunks.append(np.ascontiguousarray(raw[:, [0, 3, 6]], dtype=np.int32))
            fu_chunks.append(np.ascontiguousarray(raw[:, [1, 4, 7]], dtype=np.int32))
            fn_chunks.append(np.ascontiguousarray(raw[:, [2, 5, 8]], dtype=np.int32))
            face_count += n
        else:
            # Per-line fallback: quads, missing slots, or non-standard format.
            pos_l: list = []; uv_l: list = []; nrm_l: list = []
            for line in f_strs:
                verts = []
                for tok in line.split()[1:]:
                    parts = tok.split("/")
                    if not parts or not parts[0]:
                        continue
                    p  = int(parts[0]); p  = (p - 1) if p > 0 else p
                    u  = (int(parts[1]) - 1 if len(parts) > 1 and parts[1] else -1)
                    nm = (int(parts[2]) - 1 if len(parts) > 2 and parts[2] else -1)
                    verts.append((p, u, nm))
                for k in range(1, len(verts) - 1):
                    a, b, c = verts[0], verts[k], verts[k + 1]
                    pos_l.append((a[0], b[0], c[0]))
                    uv_l.append ((a[1], b[1], c[1]))
                    nrm_l.append((a[2], b[2], c[2]))
            if pos_l:
                fp_chunks.append(np.array(pos_l, dtype=np.int32))
                fu_chunks.append(np.array(uv_l,  dtype=np.int32))
                fn_chunks.append(np.array(nrm_l, dtype=np.int32))
                face_count += len(pos_l)
        f_strs.clear()

    _t = time.perf_counter()
    if progress_cb:
        progress_cb("parsing geometry", 0.0)

    with open(obj_path, "r", buffering=8 * 1024 * 1024, errors="replace") as fh:
        for line in fh:
            bytes_read += len(line)
            if progress_cb and file_size:
                frac = bytes_read / file_size
                if frac - last_report > 0.01:
                    progress_cb("parsing geometry", frac)
                    last_report = frac

            if line[:2] == "v ":
                v_strs.append(line[2:])
                if len(v_strs)  >= _V_BATCH: _flush_verts(v_strs,  v_chunks,  3)
            elif line[:3] == "vt ":
                vt_strs.append(line[3:])
                if len(vt_strs) >= _V_BATCH: _flush_verts(vt_strs, vt_chunks, 2)
            elif line[:3] == "vn ":
                vn_strs.append(line[3:])
                if len(vn_strs) >= _V_BATCH: _flush_verts(vn_strs, vn_chunks, 3)
            elif line[:2] == "f ":
                f_strs.append(line)
                if len(f_strs) >= _F_BATCH: _flush_faces()
            elif line[:7] == "usemtl ":
                _flush_faces()   # keep face_count current before recording event
                mat_name = line[7:].strip()
                if current_mat is not None and face_count > current_start:
                    material_ranges.append(MaterialRange(current_mat, current_start, face_count))
                current_mat   = mat_name
                current_start = face_count
            elif line[:7] == "mtllib ":
                mtl_file = line[7:].strip()

    _flush_verts(v_strs,  v_chunks,  3)
    _flush_verts(vt_strs, vt_chunks, 2)
    _flush_verts(vn_strs, vn_chunks, 3)
    _flush_faces()

    if current_mat is not None and face_count > current_start:
        material_ranges.append(MaterialRange(current_mat, current_start, face_count))

    if timing is not None:
        timing["stream_parse"] = time.perf_counter() - _t
        timing["prepass"]      = 0.0
        timing["parse_loop"]   = timing["stream_parse"]

    def _cat(chunks: list, cols: int, dtype, fill) -> np.ndarray:
        if not chunks:
            return np.full((0, cols), fill, dtype=dtype)
        return np.concatenate(chunks, axis=0)

    positions = _cat(v_chunks,  3, np.float32, 0.0)
    uvs       = _cat(vt_chunks, 2, np.float32, 0.0)
    normals   = _cat(vn_chunks, 3, np.float32, 0.0)
    face_pos_idx = _cat(fp_chunks, 3, np.int32,  0)
    face_uv_idx  = _cat(fu_chunks, 3, np.int32, -1)
    face_nrm_idx = _cat(fn_chunks, 3, np.int32, -1)

    if progress_cb:
        progress_cb("done", 1.0)

    return RawMesh(
        positions=positions, uvs=uvs, normals=normals,
        face_pos_idx=face_pos_idx, face_uv_idx=face_uv_idx, face_nrm_idx=face_nrm_idx,
        material_ranges=material_ranges, mtl_file=mtl_file,
    )


def parse_obj(obj_path: str, progress_cb=None, timing: dict | None = None) -> RawMesh:
    """
    Parse `obj_path` into a RawMesh.

    Reads the file once into memory, partitions lines by prefix in a single
    Python pass, then uses np.fromstring to bulk-convert vertex floats and
    (for uniformly-triangulated files with v/vt/vn indices) face integers at
    C speed.  Falls back to the original line-by-line loop for files that use
    quads, n-gons, or negative OBJ indices.

    progress_cb(stage: str, fraction: float) is called periodically if given.
    timing, if given, is populated with elapsed seconds for sub-stages:
      'prepass'    -- file read + line partition (compat key for printout)
      'parse_loop' -- vertex + face parse       (compat key for printout)
    """
    # Files above this threshold use the chunked streaming parser instead of
    # loading the entire file into RAM.  Override with CAVEVIEWER_BULK_PARSE_MB.
    _max_bulk_mb = int(os.environ.get("CAVEVIEWER_BULK_PARSE_MB", "2000"))
    _file_mb     = os.path.getsize(obj_path) / (1024 * 1024)
    if _file_mb > _max_bulk_mb:
        print(f"[CaveViewer] Large OBJ ({_file_mb:.0f} MB > {_max_bulk_mb} MB threshold): "
              f"using chunked streaming parser "
              f"(set CAVEVIEWER_BULK_PARSE_MB env var to raise the limit)")
        return _parse_obj_stream(obj_path, progress_cb, timing)

    if progress_cb:
        progress_cb("reading file", 0.0)

    _t = time.perf_counter()
    with open(obj_path, "r", buffering=8 * 1024 * 1024, errors="replace") as fh:
        lines = fh.readlines()
    if timing is not None:
        timing["file_read"] = time.perf_counter() - _t

    if progress_cb:
        progress_cb("partitioning lines", 0.1)

    _t = time.perf_counter()

    # Single-pass partition by line prefix.
    v_bufs:  list[str] = []
    vt_bufs: list[str] = []
    vn_bufs: list[str] = []
    f_lines: list[str] = []
    usemtl_events: list[tuple[int, str]] = []  # (face_line_idx, mat_name)
    mtl_file: str | None = None

    for line in lines:
        if line[:2] == "v ":
            v_bufs.append(line[2:])
        elif line[:3] == "vt ":
            vt_bufs.append(line[3:])
        elif line[:3] == "vn ":
            vn_bufs.append(line[3:])
        elif line[:2] == "f ":
            f_lines.append(line)
        elif line[:7] == "usemtl ":
            usemtl_events.append((len(f_lines), line[7:].strip()))
        elif line[:7] == "mtllib ":
            mtl_file = line[7:].strip()

    lines = None  # release ~file_size bytes of memory

    if timing is not None:
        timing["partition"] = time.perf_counter() - _t

    if progress_cb:
        progress_cb("parsing vertices", 0.2)

    _t = time.perf_counter()

    def _parse_floats(bufs: list, cols: int) -> np.ndarray:
        """Bulk-convert a list of whitespace-separated float lines to (N, cols)."""
        if not bufs:
            return np.zeros((0, cols), dtype=np.float32)
        return np.fromstring("".join(bufs), dtype=np.float32, sep=" ").reshape(-1, cols)

    positions = _parse_floats(v_bufs, 3);  v_bufs  = None
    uvs       = _parse_floats(vt_bufs, 2); vt_bufs = None
    normals   = _parse_floats(vn_bufs, 3); vn_bufs = None

    if timing is not None:
        timing["vertex_parse"] = time.perf_counter() - _t

    if progress_cb:
        progress_cb("parsing faces", 0.35)

    _t_face = time.perf_counter()
    n_face_lines = len(f_lines)

    # Bulk face parse: strip "f " prefix, replace "/" with " ", feed to
    # np.fromstring in batches of ~500k lines to keep peak allocation bounded.
    _BATCH = 500_000
    raw_parts: list[np.ndarray] = []
    for i in range(0, n_face_lines, _BATCH):
        chunk_text = "".join(
            line[2:].replace("/", " ") for line in f_lines[i: i + _BATCH]
        )
        raw_parts.append(np.fromstring(chunk_text, dtype=np.int32, sep=" "))
        if progress_cb:
            frac = 0.35 + 0.45 * min(i + _BATCH, n_face_lines) / max(n_face_lines, 1)
            progress_cb("parsing faces", frac)

    raw = np.concatenate(raw_parts) if raw_parts else np.zeros(0, dtype=np.int32)
    raw_parts = None

    # Fast path: every face line must produce exactly 9 integers (triangle
    # with pos/uv/nrm) using only positive 1-based indices (no negatives,
    # no missing slots).  This is guaranteed by Agisoft OBJ exports.
    use_fast = (
        n_face_lines > 0
        and raw.size == n_face_lines * 9
        and int(raw.min()) >= 1
    )

    if use_fast:
        raw = raw.reshape(n_face_lines, 9)
        raw -= 1  # 1-based -> 0-based in-place
        face_pos_idx = np.ascontiguousarray(raw[:, [0, 3, 6]], dtype=np.int32)
        face_uv_idx  = np.ascontiguousarray(raw[:, [1, 4, 7]], dtype=np.int32)
        face_nrm_idx = np.ascontiguousarray(raw[:, [2, 5, 8]], dtype=np.int32)
        n_tris = n_face_lines
        raw = None

        material_ranges: list[MaterialRange] = []
        current_mat: str | None = None
        current_start = 0
        for fi_at_event, mat_name in usemtl_events:
            if current_mat is not None and fi_at_event > current_start:
                material_ranges.append(MaterialRange(current_mat, current_start, fi_at_event))
            current_mat = mat_name
            current_start = fi_at_event
        if current_mat is not None and n_tris > current_start:
            material_ranges.append(MaterialRange(current_mat, current_start, n_tris))

    else:
        # Fallback: line-by-line with fan-triangulation, negative index
        # support, and missing uv/normal slot handling.
        face_pos_list: list = []
        face_uv_list:  list = []
        face_nrm_list: list = []
        material_ranges = []
        current_mat = None
        current_start = 0
        ev_idx = 0
        n_v, n_vt, n_vn = len(positions), len(uvs), len(normals)

        for line_i, line in enumerate(f_lines):
            # Emit usemtl events that fired at this face-line position.
            while ev_idx < len(usemtl_events) and usemtl_events[ev_idx][0] == line_i:
                _, mat_name = usemtl_events[ev_idx]
                ev_idx += 1
                fi = len(face_pos_list)
                if current_mat is not None and fi > current_start:
                    material_ranges.append(MaterialRange(current_mat, current_start, fi))
                current_mat = mat_name
                current_start = fi

            tokens = line.split()[1:]
            verts = []
            for tok in tokens:
                parts = tok.split("/")
                if not parts or not parts[0]:
                    continue
                p_idx   = _resolve_index(int(parts[0]), n_v)
                uv_idx  = _resolve_index(int(parts[1]), n_vt) if len(parts) > 1 and parts[1] else -1
                nrm_idx = _resolve_index(int(parts[2]), n_vn) if len(parts) > 2 and parts[2] else -1
                verts.append((p_idx, uv_idx, nrm_idx))

            for k in range(1, len(verts) - 1):
                a, b, c = verts[0], verts[k], verts[k + 1]
                face_pos_list.append((a[0], b[0], c[0]))
                face_uv_list.append ((a[1], b[1], c[1]))
                face_nrm_list.append((a[2], b[2], c[2]))

        n_tris = len(face_pos_list)
        if n_tris:
            face_pos_idx = np.array(face_pos_list, dtype=np.int32)
            face_uv_idx  = np.array(face_uv_list,  dtype=np.int32)
            face_nrm_idx = np.array(face_nrm_list, dtype=np.int32)
        else:
            face_pos_idx = np.zeros((0, 3), dtype=np.int32)
            face_uv_idx  = np.full((0, 3), -1, dtype=np.int32)
            face_nrm_idx = np.full((0, 3), -1, dtype=np.int32)

        if current_mat is not None and n_tris > current_start:
            material_ranges.append(MaterialRange(current_mat, current_start, n_tris))

    f_lines = None

    if timing is not None:
        timing["face_parse"] = time.perf_counter() - _t_face
        # Backward-compatible keys used by import_and_cache printout
        timing["prepass"]    = timing.get("file_read", 0) + timing.get("partition", 0)
        timing["parse_loop"] = timing.get("vertex_parse", 0) + timing.get("face_parse", 0)

    if progress_cb:
        progress_cb("done", 1.0)

    return RawMesh(
        positions=positions,
        uvs=uvs,
        normals=normals,
        face_pos_idx=face_pos_idx,
        face_uv_idx=face_uv_idx,
        face_nrm_idx=face_nrm_idx,
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

    with open(mtl_path, "r", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("newmtl "):
                if current_name is not None:
                    materials[current_name] = Material(current_name, current_tex)
                current_name = line.split(maxsplit=1)[1].strip()
                current_tex = None
            elif line.startswith("map_Kd "):
                current_tex = line.split(maxsplit=1)[1].strip().strip('"').strip("'")

    if current_name is not None:
        materials[current_name] = Material(current_name, current_tex)

    return materials
