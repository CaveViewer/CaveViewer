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

from caveviewer.core.diagnostics.application import (
    ApplicationDiagnostics,
    get_active_application_diagnostics,
    set_active_application_diagnostics,
)
from caveviewer.core.map import source_model
from caveviewer.core.release_metadata import display_version
from caveviewer.version import APP_NAME, APP_VERSION
from caveviewer.core.diagnostics.runtime import (
    RuntimeDiagnostics,
    create_runtime_diagnostics,
    get_active_runtime_diagnostics,
    record_runtime_exception,
    record_runtime_stage,
    set_active_runtime_diagnostics,
)
from caveviewer.core.diagnostics.startup import (
    StartupDiagnostics,
    get_active_startup_diagnostics,
    record_startup_stage,
    set_active_startup_diagnostics,
)
from caveviewer.core.diagnostics.logging import (
    configure_logging,
    finish_console_progress_line,
    get_logger,
    set_console_progress,
)
from caveviewer.core.preferences.runtime_settings import (
    RUNTIME_SETTING_SPECS,
    RuntimeSettings,
    RuntimeSettingsSession,
    current_runtime_platform_facts,
    resolve_runtime_settings,
    runtime_setting_spec,
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


def _record_application_event(event: str, **payload) -> None:
    """Forward an application event when process diagnostics are active."""
    diagnostics = get_active_application_diagnostics()
    if diagnostics is not None:
        diagnostics.record(event, **payload)


def _record_application_exception(event: str, exc: BaseException, **context) -> None:
    """Record a viewer-boundary exception with its traceback when enabled."""
    diagnostics = get_active_application_diagnostics()
    if diagnostics is not None:
        diagnostics.record_exception(
            event,
            type(exc),
            exc,
            exc.__traceback__,
            fatal=True,
            **context,
        )


def _attach_startup_diagnostics_logging() -> None:
    """Add the pre-splash file handler after normal root logging is configured."""

    diagnostics = get_active_startup_diagnostics()
    if diagnostics is None:
        return
    try:
        diagnostics.attach_to_root_logger()
    except Exception as error:
        diagnostics.record_exception("startup_logging_attachment_failed", error)


def _attach_runtime_diagnostics_logging() -> None:
    """Persist post-splash logs and native faults once logging is configured."""

    diagnostics = get_active_runtime_diagnostics()
    if diagnostics is None:
        return
    try:
        diagnostics.attach_to_root_logger()
        diagnostics.enable_fault_handler()
    except Exception as error:
        diagnostics.record_exception("runtime_logging_attachment_failed", error)


def _runtime_diagnostics_hint() -> str:
    """Return a concise diagnostic-file hint for a user-visible fatal error."""

    diagnostics = get_active_runtime_diagnostics()
    if diagnostics is None:
        return ""
    return f"\n\nDiagnostic log:\n{diagnostics.path}"


def _show_viewer_launch_error(error: BaseException) -> None:
    """Show a best-effort explanation for a viewer failure with no console."""

    message = (
        "CaveViewer could not start the 3D viewer.\n\n"
        f"{error}{_runtime_diagnostics_hint()}"
    )
    try:
        import tkinter as tk
        from caveviewer.gui.platform import tk_root_options
        from caveviewer.gui.notifications import show_error

        root = tk.Tk(**tk_root_options())
        root.withdraw()
        show_error(message, parent=root)
    except Exception:
        pass


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


def _runtime_diagnostic_value(value: object) -> str:
    """Format one safe resolved value without exposing unset internals."""

    return "<unset>" if value is None else str(value)


def _print_runtime_settings(runtime_settings: RuntimeSettings) -> None:
    """Log the registry's safe effective settings without inspecting ``environ``.

    The registry, rather than an ad-hoc environment-variable list, decides
    which settings are both application runtime inputs and safe for startup
    diagnostics.  Unknown process variables and settings that may contain
    private paths or URLs are deliberately omitted.
    """

    _LOG.info("CaveViewer runtime settings at startup:")
    for spec in RUNTIME_SETTING_SPECS:
        if not spec.diagnostic_safe or spec.environment_variable is None:
            continue
        _LOG.info(
            "  %s=%s (source: %s)",
            spec.environment_variable,
            _runtime_diagnostic_value(runtime_settings[spec.key]),
            runtime_settings.source(spec.key).value,
        )
    for issue in runtime_settings.issues:
        spec = runtime_setting_spec(issue.key)
        setting_name = spec.environment_variable or issue.key
        _LOG.warning(
            "Ignoring invalid runtime setting %s from %s: %s",
            setting_name,
            issue.source.value,
            issue.message,
        )


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
    source_format = source_model.source_format_for_id(model_descriptor.get("format"))

    if (
        source_format is not None
        and source_format.id is source_model.SourceFormatId.OBJ
    ):
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


def pick_folder_dialog(
    *,
    desktop_services=None,
    platform_runtime=None,
) -> str | None:
    """Open the platform directory chooser used by folder/cache workflows."""
    from caveviewer.gui.map_opening import pick_folder_dialog as _pick_folder_dialog

    return _pick_folder_dialog(
        desktop_services=desktop_services,
        platform_runtime=platform_runtime,
    )


def _print_viewer_controls() -> None:
    _LOG.info("Launching viewer...")
    _LOG.info("Controls help is available in-app via the Help button.")


def _log_cache_chunk_size(
    cache_dir: str,
    *,
    context: str = "Chunk cache",
    runtime_settings: RuntimeSettings | None = None,
) -> None:
    """Log the chunk size recorded in an existing cache manifest."""
    from caveviewer.core.chunking import builder as chunker

    cache_chunk_size = chunker.cache_chunk_size(cache_dir)
    configured_chunk_size = (
        float(runtime_settings["chunk_size_meters"])
        if runtime_settings is not None
        else chunker.configured_chunk_size()
    )
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


def _resolve_recorded_dive_selection(selected_path: str):
    """Resolve a selected JSONL trace to its local source map and trace model."""
    from caveviewer.gui.recorded_dive import (
        is_recorded_dive_path,
        load_recorded_dive_trace,
        resolve_recorded_dive_source_path,
    )

    if not is_recorded_dive_path(selected_path):
        return selected_path, None

    trace = load_recorded_dive_trace(selected_path)
    from caveviewer.gui.map_history import load_recent_map_paths

    source_path = resolve_recorded_dive_source_path(
        trace,
        search_directories=load_recent_map_paths(),
    )
    _LOG.info("Opening Recorded Dive: %s", trace.path)
    _LOG.info("Recorded Dive source map: %s", source_path)
    return os.fspath(source_path), trace


def _run_map_session(
    folder: str,
    *,
    platform_runtime=None,
    runtime_settings: RuntimeSettings | None = None,
) -> None:
    """Load and view one cave map. Returns when the viewer window closes."""
    from caveviewer.gui.recorded_dive import RecordedDiveError

    original_selection = os.path.abspath(folder)
    try:
        selected_path, recorded_dive_trace = _resolve_recorded_dive_selection(
            original_selection
        )
    except RecordedDiveError as exc:
        _LOG.error("Could not open Recorded Dive: %s", exc)
        sys.exit(1)
    selected_path = os.path.abspath(selected_path)
    selected_is_file = os.path.isfile(selected_path)
    folder = os.path.dirname(selected_path) if selected_is_file else selected_path
    _LOG.info(f"Selected map path: {selected_path}")
    _record_application_event(
        "map_session_selected",
        selected_path=selected_path,
        selected_is_file=selected_is_file,
    )
    record_runtime_stage(
        "map_session_selected",
        selected_path=selected_path,
        selected_is_file=selected_is_file,
    )

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
            _log_cache_chunk_size(
                _prebuilt_cache,
                context="Pre-compiled map cache",
                runtime_settings=runtime_settings,
            )
            _print_viewer_controls()
            from caveviewer.gui.viewer_window import run_viewer
            try:
                viewer_kwargs = {
                    "textures_dir": _textures_dir,
                    "map_root": folder,
                }
                if platform_runtime is not None:
                    viewer_kwargs["platform_runtime"] = platform_runtime
                if runtime_settings is not None:
                    viewer_kwargs["runtime_settings"] = runtime_settings
                _record_application_event(
                    "viewer_session_launch_requested",
                    launch_mode="prebuilt_cache",
                    cache_dir=_prebuilt_cache,
                    map_root=folder,
                )
                record_runtime_stage(
                    "viewer_session_launch_requested",
                    launch_mode="prebuilt_cache",
                    cache_dir=_prebuilt_cache,
                    map_root=folder,
                )
                run_viewer(_prebuilt_cache, **viewer_kwargs)
                _record_application_event(
                    "viewer_session_returned",
                    outcome="window_closed",
                    cache_dir=_prebuilt_cache,
                )
                record_runtime_stage(
                    "viewer_session_returned",
                    outcome="window_closed",
                    cache_dir=_prebuilt_cache,
                )
                from caveviewer.gui.map_history import remember_recent_map_path

                remember_recent_map_path(folder)
            except Exception as launch_err:
                _record_application_exception(
                    "viewer_session_exception",
                    outcome="exception",
                    cache_dir=_prebuilt_cache,
                    exc=launch_err,
                )
                record_runtime_exception(
                    "viewer_session_exception",
                    launch_err,
                    cache_dir=_prebuilt_cache,
                )
                _LOG.error(f"Error starting viewer: {launch_err}")
                _show_viewer_launch_error(launch_err)
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
        _log_cache_chunk_size(
            cache_dir,
            context="Existing chunk cache",
            runtime_settings=runtime_settings,
        )
        from caveviewer.gui.viewer_window import run_viewer
        try:
            viewer_kwargs = {
                "textures_dir": cache_textures_dir,
                "map_root": folder,
            }
            if platform_runtime is not None:
                viewer_kwargs["platform_runtime"] = platform_runtime
            if runtime_settings is not None:
                viewer_kwargs["runtime_settings"] = runtime_settings
            if recorded_dive_trace is not None:
                from caveviewer.gui.recorded_dive import (
                    validate_recorded_dive_manifest,
                )

                validate_recorded_dive_manifest(
                    recorded_dive_trace,
                    chunker.load_manifest(cache_dir),
                )
                viewer_kwargs["recorded_dive_trace"] = recorded_dive_trace
            _record_application_event(
                "viewer_session_launch_requested",
                launch_mode="existing_cache",
                cache_dir=cache_dir,
                map_root=folder,
            )
            record_runtime_stage(
                "viewer_session_launch_requested",
                launch_mode="existing_cache",
                cache_dir=cache_dir,
                map_root=folder,
            )
            run_viewer(cache_dir, **viewer_kwargs)
            _record_application_event(
                "viewer_session_returned",
                outcome="window_closed",
                cache_dir=cache_dir,
            )
            record_runtime_stage(
                "viewer_session_returned",
                outcome="window_closed",
                cache_dir=cache_dir,
            )
            from caveviewer.gui.map_history import remember_recent_map_path

            remember_recent_map_path(folder)
        except Exception as e:
            _record_application_exception(
                "viewer_session_exception",
                outcome="exception",
                cache_dir=cache_dir,
                exc=e,
            )
            record_runtime_exception(
                "viewer_session_exception",
                e,
                cache_dir=cache_dir,
            )
            _LOG.error(f"Error starting viewer: {e}")
            _show_viewer_launch_error(e)
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
            viewer_kwargs = {"textures_dir": folder}
            if platform_runtime is not None:
                viewer_kwargs["platform_runtime"] = platform_runtime
            if runtime_settings is not None:
                viewer_kwargs["runtime_settings"] = runtime_settings
            if recorded_dive_trace is not None:
                viewer_kwargs["recorded_dive_trace"] = recorded_dive_trace
            _record_application_event(
                "viewer_session_launch_requested",
                launch_mode="pending_import",
                cache_dir=None,
                map_root=folder,
            )
            record_runtime_stage(
                "viewer_session_launch_requested",
                launch_mode="pending_import",
                cache_dir=None,
                map_root=folder,
            )
            run_viewer_with_pending_import(model_descriptor, **viewer_kwargs)
            _record_application_event(
                "viewer_session_returned",
                outcome="window_closed",
                cache_dir=None,
            )
            record_runtime_stage(
                "viewer_session_returned",
                outcome="window_closed",
                cache_dir=None,
            )
        except Exception as e:
            _record_application_exception(
                "viewer_session_exception",
                outcome="exception",
                cache_dir=None,
                exc=e,
            )
            record_runtime_exception(
                "viewer_session_exception",
                e,
                cache_dir=None,
            )
            _LOG.error(f"Error starting viewer: {e}")
            _show_viewer_launch_error(e)
            import traceback
            traceback.print_exc()
            sys.exit(1)


def main():
    record_startup_stage("app_main_entered")
    try:
        sys.argv, _update_branch = _consume_update_branch_arg(sys.argv)
    except ValueError as e:
        _LOG.error(str(e))
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(2)

    force_update_requested = "--force-update" in sys.argv
    if force_update_requested:
        sys.argv = [a for a in sys.argv if a != "--force-update"]

    cli_overrides: dict[str, object] = {}
    if _update_branch:
        cli_overrides["update_branch"] = _update_branch
    if force_update_requested:
        cli_overrides["force_update"] = True

    # The entry point owns the mutable session reference. All consumers get
    # immutable snapshots from it; Preferences saves replace the snapshot
    # rather than assigning their values into os.environ.
    record_startup_stage("preferences_import_begin")
    from caveviewer.gui.preferences import load_saved_preference_values
    record_startup_stage("preferences_import_complete")

    record_startup_stage("runtime_settings_resolution_begin")
    platform_facts = current_runtime_platform_facts()
    logging_settings = resolve_runtime_settings(
        preferences={},
        environ=os.environ,
        cli_overrides=cli_overrides,
        platform=platform_facts,
    )
    configure_logging(str(logging_settings["log_level"]))
    _attach_runtime_diagnostics_logging()
    _attach_startup_diagnostics_logging()
    record_startup_stage("runtime_settings_resolution_complete")
    record_startup_stage("preferences_load_begin")
    runtime_settings_session = RuntimeSettingsSession(
        preferences=load_saved_preference_values(),
        environ=os.environ,
        cli_overrides=cli_overrides,
        platform=platform_facts,
    )
    runtime_settings = runtime_settings_session.snapshot
    record_startup_stage("preferences_load_complete")
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
    release_metadata = platform_facts.release_metadata
    _LOG.info(
        "Release metadata: channel=%s, source=%s%s",
        release_metadata.release_channel,
        release_metadata.source.value,
        (
            f", detail={release_metadata.diagnostic}"
            if release_metadata.diagnostic
            else ""
        ),
    )
    if _update_branch:
        _LOG.info("Using update branch override: %s", _update_branch)

    _print_runtime_settings(runtime_settings)

    # Debug flag: forces the update prompt to appear regardless of the current
    # version. CLI input is resolved before this one composition call, while
    # CAVEVIEWER_FORCE_UPDATE remains a normal environment-only setting.
    _force_update = bool(runtime_settings["force_update"])

    # Every interactive viewer path receives one process-owned runtime.  CLI
    # launches skip splash/update presentation, but still need the same
    # capability probes, policy decisions, and platform adapters as GUI mode.
    record_startup_stage("platform_runtime_import_begin")
    from caveviewer.gui.platform.runtime import create_platform_runtime
    record_startup_stage("platform_runtime_import_complete")

    # CLI argument: open that path and exit when the viewer closes.
    if len(sys.argv) > 1 and sys.argv[1].strip():
        selected_path = sys.argv.pop(1).strip()
        _run_map_session(
            selected_path,
            platform_runtime=create_platform_runtime(runtime_settings=runtime_settings),
            runtime_settings=runtime_settings,
        )
        return

    # GUI mode: show the splash screen, run the viewer, then show the
    # splash screen again so the user can open another map or exit.
    _splash_version = "0.0.0" if _force_update else display_version(
        __version__,
        release_metadata.release_channel,
    )
    record_startup_stage("splash_module_import_begin")
    from caveviewer.gui.splash_screen import show_splash_screen
    record_startup_stage("splash_module_import_complete")
    record_startup_stage("update_manager_import_begin")
    from caveviewer.gui.update_manager import UpdateManager
    record_startup_stage("update_manager_import_complete")

    record_startup_stage("platform_runtime_create_begin")
    platform_runtime = create_platform_runtime(runtime_settings=runtime_settings)
    record_startup_stage("platform_runtime_create_complete")
    record_startup_stage("update_manager_create_begin")
    update_manager = UpdateManager(
        current_version=_splash_version,
        platform_runtime=platform_runtime,
    )
    record_startup_stage("update_manager_create_complete")
    try:
        while True:
            record_startup_stage("show_splash_screen_begin")
            folder = show_splash_screen(
                program_name=APP_NAME,
                version=_splash_version,
                update_manager=update_manager,
                desktop_services=platform_runtime.desktop_services,
                platform_runtime=platform_runtime,
                runtime_settings_provider=lambda: runtime_settings_session.snapshot,
                on_preferences_saved=runtime_settings_session.replace_preferences,
            )

            if not folder:
                _LOG.info("No folder selected. Exiting.")
                return

            _run_map_session(
                folder,
                platform_runtime=platform_runtime,
                runtime_settings=runtime_settings_session.snapshot,
            )
            # Viewer closed -- loop back to a new splash backed by the same
            # process-owned update state and any in-progress download.
    finally:
        # Splash and viewer closure do not own the worker. Only final process
        # shutdown cancels a partial package and waits for its temp cleanup.
        update_manager.shutdown()


def run(*, startup_diagnostics: StartupDiagnostics | None = None) -> None:
    """Run the application and present a best-effort fatal-error dialog."""
    if startup_diagnostics is not None:
        set_active_startup_diagnostics(startup_diagnostics)
        startup_diagnostics.record("app_run_entered")
    application_diagnostics = ApplicationDiagnostics(
        metadata={
            "app_name": APP_NAME,
            "app_version": APP_VERSION,
            "python_version": sys.version.split()[0],
            "platform": sys.platform,
        }
    )
    runtime_diagnostics: RuntimeDiagnostics | None = create_runtime_diagnostics(
        session_id=application_diagnostics.session_id,
    )
    if runtime_diagnostics is not None:
        set_active_runtime_diagnostics(runtime_diagnostics)
        application_diagnostics.bind_path(
            runtime_diagnostics.jsonl_path,
            runtime_log_path=str(runtime_diagnostics.path),
        )
        runtime_diagnostics.record(
            "app_run_entered",
            app_name=APP_NAME,
            app_version=APP_VERSION,
        )
    set_active_application_diagnostics(application_diagnostics)
    application_diagnostics.install_hooks(install_signals=True)
    try:
        import multiprocessing

        multiprocessing.freeze_support()
        main()
    except KeyboardInterrupt:
        configure_logging()
        _attach_runtime_diagnostics_logging()
        _LOG.info("%s interrupted by user.", APP_NAME)
        application_diagnostics.record(
            "application_interrupted",
            reason="keyboard_interrupt",
            sync=True,
        )
        application_diagnostics.finalize(
            outcome="interrupted",
            exit_code=130,
            reason="keyboard_interrupt",
        )
        sys.exit(130)
    except SystemExit as exc:
        exit_code = _system_exit_code(exc.code)
        application_diagnostics.finalize(
            outcome="system_exit",
            exit_code=exit_code,
            reason="sys_exit",
        )
        raise
    except Exception as e:
        import traceback
        user_error = (
            f"{APP_NAME} encountered a fatal error:\n\n{e}"
            f"{_runtime_diagnostics_hint()}"
        )
        error_msg = f"{user_error}\n\nTraceback:\n{traceback.format_exc()}"
        configure_logging()
        _attach_runtime_diagnostics_logging()
        _LOG.error(error_msg)
        application_diagnostics.record_exception(
            "application_uncaught_exception",
            type(e),
            e,
            e.__traceback__,
            fatal=True,
        )

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
        application_diagnostics.finalize(
            outcome="fatal_error",
            exit_code=1,
            reason="uncaught_exception",
        )
        sys.exit(1)
    else:
        application_diagnostics.finalize(
            outcome="normal",
            exit_code=0,
            reason="main_returned",
        )
    finally:
        if get_active_application_diagnostics() is application_diagnostics:
            set_active_application_diagnostics(None)
        if runtime_diagnostics is not None:
            try:
                runtime_diagnostics.close()
            except Exception:
                pass
        if get_active_runtime_diagnostics() is runtime_diagnostics:
            set_active_runtime_diagnostics(None)
        if get_active_startup_diagnostics() is startup_diagnostics:
            set_active_startup_diagnostics(None)
        if startup_diagnostics is not None:
            startup_diagnostics.close()


def _system_exit_code(value: object) -> int | None:
    """Normalize ``SystemExit.code`` for structured process diagnostics."""
    if value is None:
        return 0
    if isinstance(value, int):
        return int(value)
    return 1


if __name__ == "__main__":
    run()
