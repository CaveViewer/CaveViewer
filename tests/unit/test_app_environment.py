"""Tests for CaveViewer startup runtime-settings diagnostics."""

from caveviewer.core.preferences import runtime_settings
from caveviewer.gui.viewer_window import CaveViewerWindow


def _resolve(tmp_path, *, platform_name="linux"):
    return runtime_settings.resolve_runtime_settings(
        environ={},
        platform=runtime_settings.RuntimePlatformFacts(
            platform_name=platform_name,
            os_name="nt" if platform_name.startswith("win") else "posix",
            home=tmp_path,
        ),
    )


def test_reported_ui_text_scale_matches_renderer_default(tmp_path):
    reported = _resolve(tmp_path)["ui_text_scale"]

    assert reported == CaveViewerWindow.UI_TEXT_SCALE


def test_reported_text_antialiasing_default_is_platform_specific(tmp_path):
    assert _resolve(tmp_path, platform_name="darwin")["text_antialiasing_mode"] == "light"

    assert _resolve(tmp_path, platform_name="linux")["text_antialiasing_mode"] == "light"


def test_diagnostic_catalog_comes_from_runtime_registry(tmp_path):
    names = {
        spec.environment_variable
        for spec in runtime_settings.RUNTIME_SETTING_SPECS
        if spec.diagnostic_safe and spec.environment_variable is not None
    }
    snapshot = _resolve(tmp_path)

    assert "CAVEVIEWER_TK_SCALE" in names
    assert "CAVEVIEWER_OBJ_BUCKET_WORKERS" in names
    assert "CAVEVIEWER_WINDOW_SYSTEM" in names
    assert "CAVEVIEWER_VIEWER_UI_SCALE" in names
    assert "CAVEVIEWER_IO_NICE" in names
    assert "CAVEVIEWER_MAP_CACHE_DIR" not in names
    assert snapshot["obj_bucket_workers"] == 2
    assert snapshot["io_nice_increment"] == 5
    assert snapshot["viewer_ui_scale"] is None
