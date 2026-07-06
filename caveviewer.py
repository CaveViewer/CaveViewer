#!/usr/bin/env python3
"""
caveviewer.py

CaveViewer entry point.

Workflow:
  1. User picks a folder containing the Agisoft export (.obj + .mtl + .jpg
     texture tiles).
  2. We find the .obj/.mtl, and check whether a valid chunk cache already
     exists (built on a previous run). If valid, skip straight to step 4.
  3. If no valid cache: parse the OBJ (streaming, handles 2GB+ files) and
     build the spatial chunk cache on disk -- this is the one-time cost
     that makes all future loads of this same map instant. Shows progress.
  4. Launch the OpenGL viewer window, which streams chunks in/out based on
     where the user flies, so frame rate stays smooth regardless of total
     map size.

Bare-bones UI for now per your request -- a Tkinter folder-picker dialog
and a console progress readout, nothing fancier. We can layer a nicer UI
on top later without touching any of the core/ engine code.
"""

import os
import sys
import glob
import time
from caveviewer_version import APP_NAME, APP_VERSION
from core.logging_utils import configure_logging, get_logger

__version__ = APP_VERSION

# Make sure 'core' and 'gui' packages are importable regardless of the
# directory this script is launched from.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Inject the OS trust store into Python's SSL before any network calls.
# This fixes CERTIFICATE_VERIFY_FAILED on Windows when antivirus or
# corporate proxy software performs SSL inspection -- those tools add
# their own root CA to the Windows certificate store, which Python's
# bundled CA bundle does not know about.  truststore makes Python use
# the same verification path as Windows itself (and Chrome/Edge).
try:
    import truststore
    truststore.inject_into_ssl()
except Exception:
    pass  # non-fatal: falls back to Python's bundled CA bundle

_UPDATE_STARTED_SENTINEL = "__caveviewer_update_started__"
_LOG = get_logger("CaveViewer")

_KNOWN_CAVEVIEWER_ENV_VARS = (
    "CAVEVIEWER_APP_ICON",
    "CAVEVIEWER_CHUNK_BUILD_RESERVED_CPUS",
    "CAVEVIEWER_CHUNK_BUILD_WORKERS",
    "CAVEVIEWER_CHUNK_SIZE_METERS",
    "CAVEVIEWER_DEV_VENV",
    "CAVEVIEWER_FORCE_STARTUP_FOCUS",
    "CAVEVIEWER_FORCE_UPDATE_PROMPT",
    "CAVEVIEWER_GITHUB_REPO",
    "CAVEVIEWER_GPU_MEMORY_GB",
    "CAVEVIEWER_GPU_MEMORY_UTILIZATION_TARGET",
    "CAVEVIEWER_HOME",
    "CAVEVIEWER_IO_RESERVED_CPUS",
    "CAVEVIEWER_IO_WORKERS",
    "CAVEVIEWER_LINUX_BUILD_VENV",
    "CAVEVIEWER_LOG_LEVEL",
    "CAVEVIEWER_MACOS_BUILD_VENV",
    "CAVEVIEWER_MEMORY_UTILIZATION_TARGET",
    "CAVEVIEWER_OBJ_SCAN_THROTTLE_MS",
    "CAVEVIEWER_TEXT_AA_MODE",
    "CAVEVIEWER_UI_FONT",
    "CAVEVIEWER_UI_TEXT_SCALE",
    "CAVEVIEWER_UPDATE_BRANCH",
    "CAVEVIEWER_UPDATE_MANIFEST_URL",
    "CAVEVIEWER_UPLOAD_CHUNKS_PER_FRAME",
    "CAVEVIEWER_UPLOAD_TIME_BUDGET_MS",
    "CAVEVIEWER_VSYNC",
)


def _console_write(text: str) -> None:
    """Best-effort console output for terminal runs; GUI launches may not have stdout."""
    stream = getattr(sys, "stdout", None)
    if stream is None:
        return
    try:
        stream.write(text)
        stream.flush()
    except Exception:
        pass


def _console_newline() -> None:
    _console_write("\n")


def _default_io_workers() -> str:
    return str(max(1, (os.cpu_count() or 1) - 3))


def _default_chunk_build_workers() -> str:
    return str(max(1, (os.cpu_count() or 1) - 2))


_CAVEVIEWER_ENV_EFFECTIVE_DEFAULTS = {
    "CAVEVIEWER_CHUNK_BUILD_RESERVED_CPUS": "2",
    "CAVEVIEWER_CHUNK_BUILD_WORKERS": _default_chunk_build_workers,
    "CAVEVIEWER_CHUNK_SIZE_METERS": "8",
    "CAVEVIEWER_GPU_MEMORY_GB": "auto-detect",
    "CAVEVIEWER_GPU_MEMORY_UTILIZATION_TARGET": "70",
    "CAVEVIEWER_IO_RESERVED_CPUS": "3",
    "CAVEVIEWER_IO_WORKERS": _default_io_workers,
    "CAVEVIEWER_MEMORY_UTILIZATION_TARGET": "12",
    "CAVEVIEWER_OBJ_SCAN_THROTTLE_MS": "1" if os.name == "nt" else "0",
    "CAVEVIEWER_TEXT_AA_MODE": "normal",
    "CAVEVIEWER_UI_TEXT_SCALE": "1.18",
    "CAVEVIEWER_UPLOAD_CHUNKS_PER_FRAME": "1",
    "CAVEVIEWER_UPLOAD_TIME_BUDGET_MS": "3.0",
}


def _effective_env_default(key: str) -> str | None:
    default = _CAVEVIEWER_ENV_EFFECTIVE_DEFAULTS.get(key)
    if default is None:
        return None
    if callable(default):
        try:
            return str(default())
        except Exception:
            return None
    return str(default)


def _print_caveviewer_environment_settings() -> None:
    """
    Print known CaveViewer environment settings without dumping unrelated
    OS/user variables that may contain secrets.
    """
    known = set(_KNOWN_CAVEVIEWER_ENV_VARS)
    discovered = {
        key
        for key, value in os.environ.items()
        if key.startswith("CAVEVIEWER_") and key not in known and str(value).strip() != ""
    }

    _LOG.info("CaveViewer environment settings at startup:")
    for key in sorted(known | discovered):
        value = os.environ.get(key)
        is_set = value is not None and str(value).strip() != ""
        display_value = value if is_set else "<unset>"
        if not is_set:
            effective_default = _effective_env_default(key)
            if effective_default is not None:
                display_value = f"{display_value} (effective: {effective_default})"
        _LOG.info(f"  {key}={display_value}")


def _consume_update_branch_arg(argv: list[str]) -> tuple[list[str], str | None]:
    cleaned: list[str] = []
    update_branch: str | None = None
    idx = 0
    while idx < len(argv):
        arg = argv[idx]
        if arg == "--update-branch":
            if idx + 1 >= len(argv) or not argv[idx + 1].strip():
                raise ValueError("--update-branch requires a non-empty branch name.")
            update_branch = argv[idx + 1].strip()
            idx += 2
            continue
        if arg.startswith("--update-branch="):
            update_branch = arg.split("=", 1)[1].strip()
            if not update_branch:
                raise ValueError("--update-branch requires a non-empty branch name.")
            idx += 1
            continue
        cleaned.append(arg)
        idx += 1
    return cleaned, update_branch


def find_input_files(folder: str) -> tuple[str, str]:
    """Locate the .obj and its .mtl inside `folder`. Returns (obj_path, mtl_path).
    Raises a clear error if the folder doesn't contain what we expect, since
    a confusing stack trace here would be a bad first impression of the tool."""
    obj_candidates = glob.glob(os.path.join(folder, "*.obj"))
    if not obj_candidates:
        raise FileNotFoundError(
            f"No .obj file found in:\n  {folder}\n\n"
            f"Make sure you selected the folder that contains the exported "
            f".obj, .mtl, and .jpg texture tiles from Agisoft."
        )
    if len(obj_candidates) > 1:
        _LOG.info(f"Note: multiple .obj files found, using the first one: {obj_candidates[0]}")
    obj_path = obj_candidates[0]

    from core.obj_parser import parse_obj  # local import; heavy-ish module

    # peek at just the mtllib line rather than a full parse, to find the mtl
    # filename quickly even on a multi-GB obj
    mtl_name = None
    with open(obj_path, "r", errors="replace") as f:
        for line in f:
            if line.startswith("mtllib "):
                mtl_name = line.split(maxsplit=1)[1].strip()
                break

    if mtl_name:
        mtl_path = os.path.join(folder, mtl_name)
        if os.path.exists(mtl_path):
            return obj_path, mtl_path

    mtl_candidates = glob.glob(os.path.join(folder, "*.mtl"))
    if not mtl_candidates:
        raise FileNotFoundError(
            f"Found {os.path.basename(obj_path)} but no matching .mtl file in:\n  {folder}"
        )
    return obj_path, mtl_candidates[0]


# Supported model file extensions, checked in this priority order when a
# folder contains more than one kind (OBJ first, since it's the original
# and most-tested format here; GLB after). A folder genuinely
# containing multiple different model formats at once is an unusual case
# this doesn't try to be clever about -- it just picks by this fixed
# priority and proceeds, the same "use the first one found" philosophy
# find_input_files already uses for multiple .obj files.
#
# NOTE: .ply support was removed -- a PLY parser was built and
# integrated but caused crashes in practice (its core API calls were
# only ever tested against hand-built fakes matching `plyfile`'s
# documented shape, never against a real install of that library, since
# the development environment had no internet access to install it).
# If PLY support is revisited later, it needs real testing against an
# actual install of `plyfile` before being wired back in here.
_SUPPORTED_EXTENSIONS = [".obj", ".glb"]


def find_model_file(folder: str) -> dict:
    """
    Format-agnostic version of find_input_files -- detects which of the
    supported model formats (.obj, .glb) a folder contains, and
    returns a small descriptor dict import_and_cache_any() can dispatch
    on, rather than forcing every format through OBJ's specific
    (obj_path, mtl_path) two-tuple shape (which doesn't make sense for
    GLB -- typically one single self-contained file with no companion at all).

    Returns one of:
      {"format": "obj", "obj_path": ..., "mtl_path": ...}
      {"format": "glb", "glb_path": ...}

    Raises FileNotFoundError if no supported model file is found at all,
    with the same kind of clear, actionable message find_input_files
    already gives for the OBJ-specific case.
    """
    for ext in _SUPPORTED_EXTENSIONS:
        candidates = glob.glob(os.path.join(folder, f"*{ext}"))
        if not candidates:
            continue
        if len(candidates) > 1:
            _LOG.info(f"Note: multiple {ext} files found, using the first one: {candidates[0]}")
        model_path = candidates[0]

        if ext == ".obj":
            obj_path, mtl_path = find_input_files(folder)
            return {"format": "obj", "obj_path": obj_path, "mtl_path": mtl_path}
        elif ext == ".glb":
            return {"format": "glb", "glb_path": model_path}

    raise FileNotFoundError(
        f"No supported model file found in:\n  {folder}\n\n"
        f"CaveViewer supports .obj (with a matching .mtl) and .glb files. "
        f"Make sure you selected the folder containing your exported map."
    )


def import_and_cache(obj_path: str, mtl_path: str, force_rebuild: bool = False,
                      extra_progress_cb=None) -> str:
    """Parse + chunk the mesh if needed, returning the cache directory.
    Skips straight to the existing cache if one's already valid, since
    re-parsing a 2GB OBJ on every launch would defeat the whole point.

    extra_progress_cb(stage: str, fraction: float), if given, is called
    alongside the built-in console progress bar at every same checkpoint
    -- this is how the OPEN button's in-window progress panel
    (gui/import_progress_panel.py) hooks into the same import process
    without needing its own separate copy of this function or changing
    the console output anyone running from a terminal already sees."""
    from core import chunker
    from core.obj_parser import parse_obj, parse_mtl

    if not force_rebuild and chunker.cache_is_valid(obj_path):
        cache_dir = chunker.get_cache_dir(obj_path)
        _LOG.info(f"Using existing chunk cache (delete the _cache "
                  f"folder next to your .obj if you want to force a rebuild).")
        _LOG.info(f"Found cache in: {cache_dir}")
        return cache_dir

    _LOG.info(f"No valid cache found -- importing {os.path.basename(obj_path)}.")
    _LOG.info("This is a one-time cost; subsequent opens of this map will be instant.")

    active_chunk_size = chunker.configured_chunk_size()
    _LOG.info(f"Using chunk size: {active_chunk_size:.1f}m "
              f"(set {chunker.CHUNK_SIZE_ENV_VAR} to override).")
    try:
        source_size_gb = os.path.getsize(obj_path) / (1024 ** 3)
        if source_size_gb >= 10.0:
            _LOG.info("Large-map tip: for very large sources, try "
                    f"{chunker.CHUNK_SIZE_ENV_VAR}=16 or 24 to reduce "
                    "chunk-file count and improve streaming performance.")
    except OSError:
        pass

    t_start = time.time()

    parse_weight = 0.5

    def _emit_progress(stage: str, frac: float):
        frac = max(0.0, min(1.0, frac))
        bar_width = 40
        filled = int(bar_width * frac)
        bar = "#" * filled + "-" * (bar_width - filled)
        _console_write(f"\r  [{bar}] {frac*100:5.1f}%  {stage:<28}")
        if extra_progress_cb:
            extra_progress_cb(stage, frac)

    def parse_progress(stage: str, frac: float):
        _emit_progress(stage, parse_weight * frac)

    def cache_progress(stage: str, frac: float):
        _emit_progress(stage, parse_weight + (1.0 - parse_weight) * frac)

    mesh = parse_obj(obj_path, progress_cb=parse_progress)
    _console_newline()  # newline after the parse progress bar

    materials = parse_mtl(mtl_path)

    target_cache_dir = os.path.join(os.path.dirname(os.path.abspath(obj_path)), chunker.CACHE_DIRNAME)
    _LOG.info(f"No reusable cache found. Building cache in: {target_cache_dir}")
    cache_dir = chunker.build_cache(obj_path, mesh, materials, progress_cb=cache_progress)
    _console_newline()

    import shutil
    _src_tex_dir = os.path.dirname(os.path.abspath(obj_path))
    for _mat in materials.values():
        if _mat.diffuse_texture:
            _src = os.path.join(_src_tex_dir, _mat.diffuse_texture)
            _dst = os.path.join(cache_dir, _mat.diffuse_texture)
            if os.path.exists(_src) and not os.path.exists(_dst):
                shutil.copy2(_src, _dst)

    elapsed = time.time() - t_start
    n_chunks = len(chunker.load_manifest(cache_dir)["chunks"])
    _LOG.info(f"Import complete in {elapsed:.1f}s -- "
              f"{len(mesh.face_pos_idx):,} triangles split into {n_chunks:,} spatial chunks.")

    return cache_dir


def import_and_cache_any(model_descriptor: dict, textures_dir: str, force_rebuild: bool = False,
                          extra_progress_cb=None) -> str:
    """
    Format-agnostic version of import_and_cache() -- dispatches on
    model_descriptor["format"] (see find_model_file()) to the right
    parser, then feeds the result into the EXACT SAME chunker.build_cache()
    used for OBJ, since core/obj_parser.py's RawMesh shape is what every
    format's parser converts into (see core/glb_parser.py's module
    docstring for the conversion details).

    The one real bridging step this function does: GLB's embedded
    texture images (raw bytes living inside the .glb file itself) get
    written out to real files inside `textures_dir` here, ONCE, during
    import -- rather than ever trying to store raw image bytes inside the
    JSON manifest (which isn't JSON-serializable anyway, and would bloat
    the manifest badly even if it were). Once written to disk, an
    embedded GLB texture is indistinguishable from an OBJ's on-disk JPEG
    from every other part of the pipeline's perspective (chunker.py,
    TextureManager reading from textures_dir, the manifest format) --
    no format-specific code needed anywhere downstream of this point.
    """
    from core import chunker
    from core.obj_parser import Material

    fmt = model_descriptor["format"]

    if fmt == "obj":
        return import_and_cache(
            model_descriptor["obj_path"], model_descriptor["mtl_path"],
            force_rebuild=force_rebuild, extra_progress_cb=extra_progress_cb,
        )

    source_path = model_descriptor["glb_path"]

    if not force_rebuild and chunker.cache_is_valid(source_path):
        cache_dir = chunker.get_cache_dir(source_path)
        _LOG.info(f"Using existing chunk cache (delete the _cache "
                  f"folder next to your {os.path.basename(source_path)} if you want to force a rebuild).")
        _LOG.info(f"Found cache in: {cache_dir}")
        return cache_dir

    _LOG.info(f"No valid cache found -- importing {os.path.basename(source_path)}.")
    _LOG.info("This is a one-time cost; subsequent opens of this map will be instant.")

    active_chunk_size = chunker.configured_chunk_size()
    _LOG.info(f"Using chunk size: {active_chunk_size:.1f}m "
              f"(set {chunker.CHUNK_SIZE_ENV_VAR} to override).")
    try:
        source_size_gb = os.path.getsize(source_path) / (1024 ** 3)
        if source_size_gb >= 10.0:
            _LOG.info("Large-map tip: for very large sources, try "
                    f"{chunker.CHUNK_SIZE_ENV_VAR}=16 or 24 to reduce "
                    "chunk-file count and improve streaming performance.")
    except OSError:
        pass

    t_start = time.time()

    parse_weight = 0.5

    def _emit_progress(stage: str, frac: float):
        frac = max(0.0, min(1.0, frac))
        bar_width = 40
        filled = int(bar_width * frac)
        bar = "#" * filled + "-" * (bar_width - filled)
        _console_write(f"\r  [{bar}] {frac*100:5.1f}%  {stage:<28}")
        if extra_progress_cb:
            extra_progress_cb(stage, frac)

    def parse_progress(stage: str, frac: float):
        _emit_progress(stage, parse_weight * frac)

    def cache_progress(stage: str, frac: float):
        _emit_progress(stage, parse_weight + (1.0 - parse_weight) * frac)

    if fmt == "glb":
        from core.glb_parser import parse_glb
        mesh, embedded_textures = parse_glb(source_path, progress_cb=parse_progress)

        # Write each embedded texture out to a real file in textures_dir,
        # once, so every downstream consumer (chunker.py's manifest,
        # TextureManager) just sees an ordinary on-disk filename -- see
        # this function's own docstring for why this is done here rather
        # than threading raw bytes through the manifest/cache format.
        materials = {}
        for mat_range in mesh.material_ranges:
            mat_name = mat_range.material_name
            if mat_name in embedded_textures:
                image_bytes = embedded_textures[mat_name]
                image_filename = _write_embedded_texture_to_disk(
                    image_bytes, textures_dir, mat_name
                )
                materials[mat_name] = Material(name=mat_name, diffuse_texture=image_filename)
            else:
                # no embedded texture found for this material under this
                # name -- leave it untextured (the placeholder-texture
                # path in TextureManager handles this the same as an
                # OBJ material with no map_Kd line)
                materials[mat_name] = Material(name=mat_name, diffuse_texture=None)

    else:
        raise ValueError(f"Unknown model format: {fmt!r}")

    _console_newline()  # newline after the parse progress bar

    target_cache_dir = os.path.join(os.path.dirname(os.path.abspath(source_path)), chunker.CACHE_DIRNAME)
    _LOG.info(f"No reusable cache found. Building cache in: {target_cache_dir}")
    cache_dir = chunker.build_cache(source_path, mesh, materials, progress_cb=cache_progress)
    _console_newline()

    import shutil
    for _mat in materials.values():
        if _mat.diffuse_texture:
            _src = os.path.join(textures_dir, _mat.diffuse_texture)
            _dst = os.path.join(cache_dir, _mat.diffuse_texture)
            if os.path.exists(_src) and not os.path.exists(_dst):
                shutil.copy2(_src, _dst)

    elapsed = time.time() - t_start
    n_chunks = len(chunker.load_manifest(cache_dir)["chunks"])
    _LOG.info(f"Import complete in {elapsed:.1f}s -- "
              f"{len(mesh.face_pos_idx):,} triangles split into {n_chunks:,} spatial chunks.")

    return cache_dir


def _write_embedded_texture_to_disk(image_bytes: bytes, textures_dir: str, material_name: str) -> str:
    """
    Writes one GLB-embedded texture's raw bytes to a real file inside
    textures_dir, sniffing the actual image format from the bytes
    themselves (JPEG vs PNG, the two formats glTF supports for textures)
    rather than trusting any file extension, since embedded image data
    has no filename of its own to go by -- just the bytes. Returns the
    filename (not full path) that was written, which the caller stores
    as that material's diffuse_texture, the same as an OBJ's .mtl
    map_Kd line would.
    """
    # JPEG files start with FF D8; PNG files start with the fixed 8-byte
    # PNG signature -- checking the actual leading bytes is more reliable
    # than guessing from context, since glTF doesn't store a format tag
    # separately from the image bytes themselves for embedded images.
    if image_bytes[:2] == b"\xff\xd8":
        ext = ".jpg"
    elif image_bytes[:8] == b"\x89PNG\r\n\x1a\n":
        ext = ".png"
    else:
        # unrecognized format -- write it anyway with a generic
        # extension; Pillow can often still sniff the real format from
        # content when TextureManager later opens it, and if it truly
        # can't, that degrades to the existing missing/corrupt-texture
        # placeholder path rather than crashing here at write time.
        ext = ".img"

    filename = f"{material_name}{ext}"
    os.makedirs(textures_dir, exist_ok=True)
    with open(os.path.join(textures_dir, filename), "wb") as f:
        f.write(image_bytes)
    return filename


def pick_folder_dialog() -> str | None:
    """Tkinter native folder picker. Tkinter ships with standard Python on
    Windows/Mac, so this needs no extra install for the bare-bones UI."""
    import tkinter as tk
    from tkinter import filedialog
    from gui.dpi_utils import apply_tk_scaling, configure_process_dpi_awareness

    configure_process_dpi_awareness()
    root = tk.Tk(className=APP_NAME)
    apply_tk_scaling(root)
    root.withdraw()
    folder = filedialog.askdirectory(
        title="Select folder containing your cave map (.obj, .mtl, .jpg)"
    )
    root.destroy()
    return folder or None


def _print_viewer_controls() -> None:
    _LOG.info("Launching viewer...")
    _LOG.info("Controls help is available in-app via the Help button.")


def _log_cache_chunk_size(cache_dir: str, *, context: str = "Chunk cache") -> None:
    """Log the chunk size recorded in an existing cache manifest."""
    from core import chunker

    cache_chunk_size = chunker.cache_chunk_size(cache_dir)
    configured_chunk_size = chunker.configured_chunk_size()
    if cache_chunk_size is None:
        _LOG.warning(
            f"{context} does not report a valid chunk size in manifest.json; "
            "opening may fail if the cache is incomplete or from an unsupported version."
        )
        return

    _LOG.info(f"{context} chunk size: {cache_chunk_size:g}m.")
    if abs(cache_chunk_size - configured_chunk_size) > 1e-6:
        _LOG.info(
            f"Current {chunker.CHUNK_SIZE_ENV_VAR} setting is {configured_chunk_size:g}m, "
            "but existing/prebuilt caches always open with their manifest chunk size. "
            "Delete or rebuild _cache to apply a different import chunk size."
        )


def _run_map_session(folder: str) -> None:
    """Load and view one cave map. Returns when the viewer window closes."""
    folder = os.path.abspath(folder)
    _LOG.info(f"Selected folder: {folder}")

    try:
        model_descriptor = find_model_file(folder)
    except FileNotFoundError as e:
        from core import chunker as _ck
        # Case 1: folder contains a _cache/ subfolder (standard layout)
        _prebuilt_cache = os.path.join(folder, _ck.CACHE_DIRNAME)
        _legacy_prebuilt_cache = os.path.join(folder, _ck.LEGACY_CACHE_DIRNAME)
        _textures_dir = folder
        # Case 2: folder itself is the cache directory (e.g. renamed or moved)
        if not os.path.exists(os.path.join(_prebuilt_cache, _ck.MANIFEST_NAME)):
            if os.path.exists(os.path.join(_legacy_prebuilt_cache, _ck.MANIFEST_NAME)):
                _LOG.info(f"Found legacy cache in: {_legacy_prebuilt_cache}")
                _prebuilt_cache = _legacy_prebuilt_cache
            elif os.path.exists(os.path.join(folder, _ck.MANIFEST_NAME)):
                _LOG.info(f"Found cache manifest in selected directory: {folder}")
                _prebuilt_cache = folder
                _textures_dir = folder
        if os.path.exists(os.path.join(_prebuilt_cache, _ck.MANIFEST_NAME)):
            _LOG.info("Pre-compiled map detected -- launching viewer directly.")
            _LOG.info("(Delete the _cache folder to force a rebuild.)")
            _LOG.info(f"Using cache directory: {_prebuilt_cache}")
            _log_cache_chunk_size(_prebuilt_cache, context="Pre-compiled map cache")
            _print_viewer_controls()
            from gui.viewer_window import run_viewer
            try:
                run_viewer(_prebuilt_cache, textures_dir=_textures_dir)
            except Exception as launch_err:
                _LOG.error(f"Error starting viewer: {launch_err}")
                import traceback
                traceback.print_exc()
                sys.exit(1)
            return
        _LOG.error(f"Error: {e}")
        sys.exit(1)

    fmt = model_descriptor["format"]
    source_path = model_descriptor.get("obj_path") or model_descriptor.get("glb_path")
    _LOG.info(f"Found {fmt.upper()} mesh: {os.path.basename(source_path)}")
    if fmt == "obj":
        _LOG.info(f"Found materials: {os.path.basename(model_descriptor['mtl_path'])}")

    _print_viewer_controls()

    from core import chunker

    if chunker.cache_is_valid(source_path):
        # Fast path, unchanged: a cache already exists, so there's no
        # import to show progress for -- launch straight in, same as
        # this has always worked.
        _LOG.info("Using existing chunk cache (delete the _cache "
                  "folder next to your model file if you want to force a rebuild).")
        cache_dir = chunker.get_cache_dir(source_path)
        _LOG.info(f"Using cache directory: {cache_dir}")
        _log_cache_chunk_size(cache_dir, context="Existing chunk cache")
        from gui.viewer_window import run_viewer
        try:
            run_viewer(cache_dir, textures_dir=folder)
        except Exception as e:
            _LOG.error(f"Error starting viewer: {e}")
            import traceback
            traceback.print_exc()
            sys.exit(1)
    else:
        # No cache yet -- open the window FIRST, with no map loaded, and
        # let it run the import itself once it's actually on screen (see
        # gui/viewer_window.py's _run_pending_import), so the same
        # in-window progress panel the OPEN button uses can show real
        # progress here too, instead of the import running to completion
        # before any window exists (which could only show a plain console
        # progress bar).
        from gui.viewer_window import run_viewer_with_pending_import
        try:
            run_viewer_with_pending_import(model_descriptor, textures_dir=folder)
        except Exception as e:
            _LOG.error(f"Error starting viewer: {e}")
            import traceback
            traceback.print_exc()
            sys.exit(1)


def main():
    configure_logging()
    if os.name == "nt":
        try:
            from gui.dpi_utils import configure_process_dpi_awareness
            configure_process_dpi_awareness()
        except Exception:
            pass
    _LOG.info("=" * 60)
    _LOG.info(f"  {APP_NAME} {__version__}")
    _LOG.info("=" * 60)

    try:
        sys.argv, _update_branch = _consume_update_branch_arg(sys.argv)
    except ValueError as e:
        _LOG.error(str(e))
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(2)
    if _update_branch:
        os.environ["CAVEVIEWER_UPDATE_BRANCH"] = _update_branch
        _LOG.info("Using update branch override: %s", _update_branch)

    _print_caveviewer_environment_settings()

    # Debug flag: forces the update prompt to appear regardless of the
    # current version.  Useful for testing the update notification UI
    # without waiting for CDN cache or editing version numbers.
    # Usage: ./run_caveviewer.sh --force-update-prompt
    #        CAVEVIEWER_FORCE_UPDATE_PROMPT=1 ./run_caveviewer.sh
    #        ./run_caveviewer.sh --update-branch feature/pubkey
    _force_update_prompt = (
        "--force-update-prompt" in sys.argv
        or os.getenv("CAVEVIEWER_FORCE_UPDATE_PROMPT", "").strip()
        in ("1", "true", "yes")
    )
    if _force_update_prompt:
        sys.argv = [a for a in sys.argv if a != "--force-update-prompt"]

    # CLI argument: open that path and exit when the viewer closes.
    if len(sys.argv) > 1 and sys.argv[1].strip():
        _run_map_session(sys.argv[1].strip())
        return

    # GUI mode: show the splash screen, run the viewer, then show the
    # splash screen again so the user can open another map or exit.
    _splash_version = "0.0.0" if _force_update_prompt else __version__
    while True:
        from gui.splash_screen import show_splash_screen
        folder = show_splash_screen(program_name=APP_NAME, version=_splash_version)

        if folder == _UPDATE_STARTED_SENTINEL:
            _LOG.info("Update is being installed; exiting the current instance.")
            return

        if not folder:
            _LOG.info("No folder selected. Exiting.")
            return

        _run_map_session(folder)
        # Viewer closed -- loop back and show the splash screen again


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        import traceback
        error_msg = f"Fatal error in CaveViewer: {e}\n\nTraceback:\n{traceback.format_exc()}"
        configure_logging()
        _LOG.error(error_msg)
        
        # Try to show error dialog if GUI is available
        try:
            import tkinter as tk
            from tkinter import messagebox
            root = tk.Tk(className=APP_NAME)
            root.withdraw()
            messagebox.showerror("CaveViewer Error", error_msg)
        except Exception:
            pass
        
        sys.exit(1)
