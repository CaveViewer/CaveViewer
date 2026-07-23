#!/usr/bin/env python3
"""
caveviewer.app

CaveViewer entry point.

Workflow:
  1. User picks a map folder, or launches a direct .glb/.obj file from the
     desktop shell or CLI.
  2. We find the source model, and check whether a valid chunk cache already
     exists (built on a previous run). If valid, skip straight to step 4.
  3. If no valid cache: parse the model and build the spatial chunk cache on
     disk -- this is the one-time cost that makes all future loads of this
     same map instant. Shows progress.
  4. Launch the OpenGL viewer window, which streams chunks in/out based on
     where the user flies, so frame rate stays smooth regardless of total
     map size.

[magic_mr_v] $ _
"""

import logging
import os
import sys
from caveviewer.version import APP_NAME, APP_VERSION
from caveviewer.core.map import source_model
from caveviewer.core.diagnostics.logging import (
    configure_logging,
    finish_console_progress_line,
    get_logger,
    set_console_progress,
)

__version__ = APP_VERSION

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

_LOG = get_logger("CaveViewer")


def _route_moderngl_window_logging() -> None:
    """
    Keep moderngl-window logs on CaveViewer's configured root handlers.

    This is intentionally in the application layer, not ``caveviewer.core``:
    moderngl-window is part of the viewer/presentation stack, while core
    logging must remain independent of GUI/OpenGL-adjacent libraries.
    """
    try:
        import moderngl_window
    except Exception:
        return

    def adopt_logger() -> None:
        logger = logging.getLogger("moderngl_window")
        logger.handlers.clear()
        logger.propagate = True
        logger.setLevel(logging.NOTSET)

    def setup_basic_logging(_level: int | None) -> None:
        adopt_logger()

    try:
        moderngl_window.setup_basic_logging = setup_basic_logging
    except Exception:
        pass
    adopt_logger()

_KNOWN_CAVEVIEWER_ENV_VARS = (
    "CAVEVIEWER_APP_ICON",
    "CAVEVIEWER_CHUNK_BUILD_RESERVED_CPUS",
    "CAVEVIEWER_CHUNK_BUILD_WORKERS",
    "CAVEVIEWER_CHUNK_SIZE_METERS",
    "CAVEVIEWER_DEV_VENV",
    "CAVEVIEWER_FORCE_STARTUP_FOCUS",
    "CAVEVIEWER_FORCE_UPDATE",
    "CAVEVIEWER_GITHUB_REPO",
    "CAVEVIEWER_GPU_MEMORY_GB",
    "CAVEVIEWER_GPU_MEMORY_UTILIZATION_TARGET",
    "CAVEVIEWER_HOME",
    "CAVEVIEWER_IMPORT_NICE",
    "CAVEVIEWER_IO_RESERVED_CPUS",
    "CAVEVIEWER_IO_WORKERS",
    "CAVEVIEWER_LINUX_BUILD_VENV",
    "CAVEVIEWER_LOG_LEVEL",
    "CAVEVIEWER_MACOS_BUILD_VENV",
    "CAVEVIEWER_MAP_CACHE_DIR",
    "CAVEVIEWER_MAX_UPLOAD_GROUP_MB",
    "CAVEVIEWER_MEMORY_UTILIZATION_TARGET",
    "CAVEVIEWER_OBJ_BUCKET_WORKERS",
    "CAVEVIEWER_OBJ_IMPORT_BATCH_FACES",
    "CAVEVIEWER_OBJ_SCAN_THROTTLE_MS",
    "CAVEVIEWER_PROJECT_ROOT",
    "CAVEVIEWER_TEXT_AA_MODE",
    "CAVEVIEWER_TK_SCALE",
    "CAVEVIEWER_UI_FONT",
    "CAVEVIEWER_UI_TEXT_SCALE",
    "CAVEVIEWER_UPDATE_BRANCH",
    "CAVEVIEWER_UPDATE_CHANNEL",
    "CAVEVIEWER_UPDATE_MANIFEST_URL",
    "CAVEVIEWER_UPLOAD_CHUNKS_PER_FRAME",
    "CAVEVIEWER_UPLOAD_GROUPS_PER_FRAME",
    "CAVEVIEWER_UPLOAD_TIME_BUDGET_MS",
    "CAVEVIEWER_VIEWER_UI_SCALE",
    "CAVEVIEWER_VSYNC",
    "CAVEVIEWER_WINDOW_SYSTEM",
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
    if not finish_console_progress_line():
        _console_write("\n")


def _make_import_progress_callback(extra_progress_cb=None, *, console_progress: bool = True):
    progress_was_rendered = False

    def _emit_progress(stage: str, frac: float) -> None:
        nonlocal progress_was_rendered
        frac = max(0.0, min(1.0, float(frac)))
        progress_was_rendered = True
        if console_progress:
            set_console_progress(stage, frac)
        if extra_progress_cb:
            extra_progress_cb(stage, frac)

    def _finish_progress() -> None:
        if console_progress and progress_was_rendered:
            _console_newline()

    return _emit_progress, _finish_progress


def _default_io_workers() -> str:
    return "2"


def _default_chunk_build_workers() -> str:
    return "1"


def _default_text_aa_mode() -> str:
    return "light" if sys.platform == "darwin" or sys.platform.startswith("linux") else "normal"


_CAVEVIEWER_ENV_EFFECTIVE_DEFAULTS = {
    "CAVEVIEWER_CHUNK_BUILD_RESERVED_CPUS": "2",
    "CAVEVIEWER_CHUNK_BUILD_WORKERS": _default_chunk_build_workers,
    "CAVEVIEWER_CHUNK_SIZE_METERS": "50",
    "CAVEVIEWER_MAX_UPLOAD_GROUP_MB": "16",
    "CAVEVIEWER_GPU_MEMORY_GB": "auto-detect",
    "CAVEVIEWER_GPU_MEMORY_UTILIZATION_TARGET": "70",
    "CAVEVIEWER_IMPORT_NICE": "5",
    "CAVEVIEWER_IO_RESERVED_CPUS": "3",
    "CAVEVIEWER_IO_WORKERS": _default_io_workers,
    "CAVEVIEWER_MEMORY_UTILIZATION_TARGET": "8",
    "CAVEVIEWER_OBJ_BUCKET_WORKERS": "2",
    "CAVEVIEWER_OBJ_IMPORT_BATCH_FACES": "200000",
    "CAVEVIEWER_OBJ_SCAN_THROTTLE_MS": "1" if os.name == "nt" else "0",
    "CAVEVIEWER_TEXT_AA_MODE": _default_text_aa_mode,
    "CAVEVIEWER_UI_TEXT_SCALE": "1.28",
    "CAVEVIEWER_UPLOAD_CHUNKS_PER_FRAME": "1",
    "CAVEVIEWER_UPLOAD_GROUPS_PER_FRAME": "1",
    "CAVEVIEWER_UPLOAD_TIME_BUDGET_MS": "3.0",
    "CAVEVIEWER_VIEWER_UI_SCALE": "auto",
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
    return source_model.find_input_files(folder, logger=_LOG)


def _find_material_file_for_obj(obj_path: str) -> str:
    """Return the material file referenced by or adjacent to one OBJ file."""
    return source_model.find_material_file_for_obj(obj_path)


def find_model_file(folder: str) -> dict:
    """
    Format-agnostic version of find_input_files -- detects which of the
    supported model formats (.obj, .glb) a folder contains, or accepts one
    directly-selected .obj/.glb file from the desktop shell. It returns a small
    descriptor dict import_and_cache_any() can dispatch on, rather than forcing
    every format through OBJ's specific (obj_path, mtl_path) two-tuple shape
    (which doesn't make sense for GLB -- typically one single self-contained
    file with no companion at all).

    Returns one of:
      {"format": "obj", "obj_path": ..., "mtl_path": ...}
      {"format": "glb", "glb_path": ...}

    Raises FileNotFoundError if no supported model file is found at all,
    with the same kind of clear, actionable message find_input_files
    already gives for the OBJ-specific case.
    """
    return source_model.find_model_file(folder, logger=_LOG)


def import_and_cache(obj_path: str, mtl_path: str, force_rebuild: bool = False,
                      extra_progress_cb=None, *, console_progress: bool = True,
                      pause_requested=None,
                      chunk_size: float | None = None) -> str:
    """Parse + chunk the mesh if needed, returning the cache directory.
    Skips straight to the existing cache if one's already valid, since
    re-parsing a 2GB OBJ on every launch would defeat the whole point.

    extra_progress_cb(stage: str, fraction: float), if given, is called
    alongside the built-in console progress bar at every same checkpoint
    -- this is how the OPEN button's in-window progress panel
    (caveviewer.gui.import_progress_panel) hooks into the same import process
    without needing its own separate copy of this function or changing
    the console output anyone running from a terminal already sees."""
    progress_cb, finish_progress = _make_import_progress_callback(
        extra_progress_cb,
        console_progress=console_progress,
    )
    try:
        from caveviewer.core.map import importer

        return importer.import_and_cache(
            obj_path,
            mtl_path,
            force_rebuild=force_rebuild,
            progress_cb=progress_cb,
            pause_requested=pause_requested,
            chunk_size=chunk_size,
        )
    finally:
        finish_progress()


def import_and_cache_any(
    model_descriptor: dict,
    textures_dir: str,
    force_rebuild: bool = False,
    extra_progress_cb=None,
    *,
    console_progress: bool = True,
    pause_requested=None,
    chunk_size: float | None = None,
) -> str:
    """
    Format-agnostic version of import_and_cache() -- dispatches on
    model_descriptor["format"] (see find_model_file()) to the right
    parser/cache path. OBJ uses the incremental disk-bucket builder so it does
    not retain whole-model face arrays; GLB still feeds its parsed RawMesh into
    chunking builder because GLB parsing already materializes that mesh.

    GLB's embedded texture bytes are named here and handed to the cache
    builder as staged assets. Once the complete cache is published, every
    downstream consumer sees ordinary files beside the manifest without the
    source folder ever needing to be writable.
    """
    fmt = model_descriptor["format"]

    if fmt == "obj":
        return import_and_cache(
            model_descriptor["obj_path"], model_descriptor["mtl_path"],
            force_rebuild=force_rebuild,
            extra_progress_cb=extra_progress_cb,
            console_progress=console_progress,
            pause_requested=pause_requested,
            chunk_size=chunk_size,
        )
    progress_cb, finish_progress = _make_import_progress_callback(
        extra_progress_cb,
        console_progress=console_progress,
    )
    try:
        from caveviewer.core.map import importer

        return importer.import_and_cache_any(
            model_descriptor,
            textures_dir,
            force_rebuild=force_rebuild,
            progress_cb=progress_cb,
            pause_requested=pause_requested,
            chunk_size=chunk_size,
        )
    finally:
        finish_progress()


def _file_texture_assets(materials: dict, textures_dir: str):
    """Return unique on-disk textures for atomic cache publication."""
    from caveviewer.core.map import importer

    return importer.file_texture_assets(materials, textures_dir)


def _embedded_texture_filename(image_bytes: bytes, material_name: str) -> str:
    """Choose a deterministic extension for an embedded GLB texture."""
    from caveviewer.core.map import importer

    return importer.embedded_texture_filename(image_bytes, material_name)


def pick_folder_dialog(*, desktop_services=None) -> str | None:
    """Open the platform directory chooser used by folder/cache workflows."""
    from caveviewer.gui.map_opening import pick_folder_dialog as _pick_folder_dialog

    return _pick_folder_dialog(desktop_services=desktop_services)


def _print_viewer_controls() -> None:
    _LOG.info("Launching viewer...")
    _LOG.info("Controls help is available in-app via the Help button.")


def _log_cache_chunk_size(cache_dir: str, *, context: str = "Chunk cache") -> None:
    """Log the chunk size recorded in an existing cache manifest."""
    from caveviewer.core.chunking import builder as chunker

    cache_chunk_size = chunker.cache_chunk_size(cache_dir)
    configured_chunk_size = chunker.configured_chunk_size()
    if cache_chunk_size is None:
        _LOG.warning(
            f"{context} does not report a valid chunk size in manifest.json; "
            "opening may fail if the cache is incomplete or from an unsupported version."
        )
        return

    _LOG.info(f"{context} chunk size: {cache_chunk_size:g}.")
    if abs(cache_chunk_size - configured_chunk_size) > 1e-6:
        _LOG.info(
            f"Current {chunker.CHUNK_SIZE_ENV_VAR} setting is {configured_chunk_size:g}, "
            "but existing/prebuilt caches always open with their manifest chunk size. "
            "Rebuild the reported cache directory to apply a different import chunk size."
        )


def _run_map_session(folder: str) -> None:
    """Load and view one cave map. Returns when the viewer window closes."""
    selected_path = os.path.abspath(folder)
    selected_is_file = os.path.isfile(selected_path)
    folder = os.path.dirname(selected_path) if selected_is_file else selected_path
    _LOG.info(f"Selected map path: {selected_path}")

    try:
        model_descriptor = find_model_file(selected_path)
    except FileNotFoundError as e:
        if selected_is_file:
            _LOG.error(f"Error: {e}")
            sys.exit(1)
        from caveviewer.core.chunking import builder as _ck
        _textures_dir = folder
        _prebuilt_cache = folder
        if os.path.exists(os.path.join(_prebuilt_cache, _ck.MANIFEST_NAME)):
            _LOG.info(f"Found cache manifest in selected directory: {folder}")
            _LOG.info("Pre-compiled map detected -- launching viewer directly.")
            _LOG.info(
                "(Delete the reported cache directory to force a rebuild.)"
            )
            _LOG.info(f"Using cache directory: {_prebuilt_cache}")
            _log_cache_chunk_size(_prebuilt_cache, context="Pre-compiled map cache")
            _print_viewer_controls()
            from caveviewer.gui.viewer_window import run_viewer
            try:
                run_viewer(_prebuilt_cache, textures_dir=_textures_dir)
                from caveviewer.gui.map_history import remember_recent_map_path

                remember_recent_map_path(folder)
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

    from caveviewer.core.chunking import builder as chunker

    if chunker.cache_is_valid(source_path):
        # Fast path, unchanged: a cache already exists, so there's no
        # import to show progress for -- launch straight in, same as
        # this has always worked.
        _LOG.info(
            "Using an existing chunk cache; remove the reported cache directory "
            "to force a rebuild."
        )
        cache_dir = chunker.get_cache_dir(source_path)
        from caveviewer.core.map.cache_paths import map_texture_dir
        cache_textures_dir = map_texture_dir(source_path, cache_dir, folder)
        _LOG.info(f"Using cache directory: {cache_dir}")
        _log_cache_chunk_size(cache_dir, context="Existing chunk cache")
        from caveviewer.gui.viewer_window import run_viewer
        try:
            run_viewer(cache_dir, textures_dir=cache_textures_dir)
            from caveviewer.gui.map_history import remember_recent_map_path

            remember_recent_map_path(folder)
        except Exception as e:
            _LOG.error(f"Error starting viewer: {e}")
            import traceback
            traceback.print_exc()
            sys.exit(1)
    else:
        # No cache yet -- open the window FIRST, with no map loaded, and
        # let it run the import itself once it's actually on screen (see
        # caveviewer.gui.viewer_window's _run_pending_import), so the same
        # in-window progress panel the OPEN button uses can show real
        # progress here too, instead of the import running to completion
        # before any window exists (which could only show a plain console
        # progress bar).
        from caveviewer.gui.viewer_window import run_viewer_with_pending_import
        try:
            run_viewer_with_pending_import(model_descriptor, textures_dir=folder)
        except Exception as e:
            _LOG.error(f"Error starting viewer: {e}")
            import traceback
            traceback.print_exc()
            sys.exit(1)


def main():
    configure_logging()
    _route_moderngl_window_logging()
    if os.name == "nt":
        try:
            from caveviewer.gui.dpi_utils import configure_process_dpi_awareness
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

    # Debug flag: forces the update prompt to appear regardless of the current
    # version.
    # Usage: ./run_caveviewer.sh --force-update
    #        CAVEVIEWER_FORCE_UPDATE=1 ./run_caveviewer.sh
    #        ./run_caveviewer.sh --update-branch feature/pubkey
    _force_update = (
        "--force-update" in sys.argv
        or os.getenv("CAVEVIEWER_FORCE_UPDATE", "").strip()
        in ("1", "true", "yes")
    )
    if _force_update:
        sys.argv = [a for a in sys.argv if a != "--force-update"]

    # CLI argument: open that path and exit when the viewer closes.
    if len(sys.argv) > 1 and sys.argv[1].strip():
        selected_path = sys.argv.pop(1).strip()
        _run_map_session(selected_path)
        return

    # GUI mode: show the splash screen, run the viewer, then show the
    # splash screen again so the user can open another map or exit.
    _splash_version = "0.0.0" if _force_update else __version__
    from caveviewer.gui.splash_screen import show_splash_screen
    from caveviewer.gui.update_manager import UpdateManager

    update_manager = UpdateManager(current_version=_splash_version)
    try:
        while True:
            folder = show_splash_screen(
                program_name=APP_NAME,
                version=_splash_version,
                update_manager=update_manager,
            )

            if not folder:
                _LOG.info("No folder selected. Exiting.")
                return

            _run_map_session(folder)
            # Viewer closed -- loop back to a new splash backed by the same
            # process-owned update state and any in-progress download.
    finally:
        # Splash and viewer closure do not own the worker. Only final process
        # shutdown cancels a partial package and waits for its temp cleanup.
        update_manager.shutdown()


def run() -> None:
    """Run the application and present a best-effort fatal-error dialog."""
    try:
        import multiprocessing

        multiprocessing.freeze_support()
        main()
    except KeyboardInterrupt:
        configure_logging()
        _LOG.info("%s interrupted by user.", APP_NAME)
        sys.exit(130)
    except Exception as e:
        import traceback
        user_error = f"{APP_NAME} encountered a fatal error:\n\n{e}"
        error_msg = f"{user_error}\n\nTraceback:\n{traceback.format_exc()}"
        configure_logging()
        _LOG.error(error_msg)
        
        # Try to show error dialog if GUI is available
        try:
            import tkinter as tk
            from caveviewer.gui.platform import tk_root_options
            from caveviewer.gui.notifications import show_error

            root = tk.Tk(**tk_root_options())
            root.withdraw()
            show_error(user_error, parent=root)
        except Exception:
            pass
        
        sys.exit(1)


if __name__ == "__main__":
    run()
