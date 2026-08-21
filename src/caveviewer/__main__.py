"""Module entry point for ``python -m caveviewer``."""

from caveviewer.core.diagnostics.startup import create_startup_diagnostics


def run() -> None:
    """Start the app after arming Windows-only diagnostics before app imports."""

    startup_diagnostics = create_startup_diagnostics()
    if startup_diagnostics is not None:
        startup_diagnostics.record("app_import_begin")
    try:
        from caveviewer.app import run as run_application
    except Exception as error:
        if startup_diagnostics is not None:
            startup_diagnostics.record_exception("app_import_failed", error)
            startup_diagnostics.close()
        raise

    if startup_diagnostics is not None:
        startup_diagnostics.record("app_import_complete")
    try:
        run_application(startup_diagnostics=startup_diagnostics)
    finally:
        if startup_diagnostics is not None:
            startup_diagnostics.close()


if __name__ == "__main__":
    run()
