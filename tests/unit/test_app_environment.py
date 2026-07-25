"""Tests for CaveViewer startup environment diagnostics."""

from caveviewer import app
from caveviewer.gui.viewer_window import CaveViewerWindow


def test_reported_ui_text_scale_matches_renderer_default():
    reported = app._effective_env_default("CAVEVIEWER_UI_TEXT_SCALE")

    assert reported is not None
    assert float(reported) == CaveViewerWindow.UI_TEXT_SCALE


def test_reported_text_antialiasing_default_is_platform_specific(monkeypatch):
    monkeypatch.setattr(app.sys, "platform", "darwin")
    assert app._effective_env_default("CAVEVIEWER_TEXT_AA_MODE") == "light"

    monkeypatch.setattr(app.sys, "platform", "linux")
    assert app._effective_env_default("CAVEVIEWER_TEXT_AA_MODE") == "light"


def test_tk_scale_is_a_known_runtime_setting():
    assert "CAVEVIEWER_TK_SCALE" in app._KNOWN_CAVEVIEWER_ENV_VARS
    assert "CAVEVIEWER_MAP_CACHE_DIR" in app._KNOWN_CAVEVIEWER_ENV_VARS
    assert "CAVEVIEWER_OBJ_BUCKET_WORKERS" in app._KNOWN_CAVEVIEWER_ENV_VARS
    assert "CAVEVIEWER_WINDOW_SYSTEM" in app._KNOWN_CAVEVIEWER_ENV_VARS
    assert "CAVEVIEWER_VIEWER_UI_SCALE" in app._KNOWN_CAVEVIEWER_ENV_VARS
    assert (
        "CAVEVIEWER_AUTO_DIVE_SMOOTHING_RADIUS_CELLS"
        in app._KNOWN_CAVEVIEWER_ENV_VARS
    )
    assert "CAVEVIEWER_AUTO_DIVE_DIAGNOSTICS" in app._KNOWN_CAVEVIEWER_ENV_VARS
    assert app._effective_env_default("CAVEVIEWER_OBJ_BUCKET_WORKERS") == "2"
    assert app._effective_env_default("CAVEVIEWER_VIEWER_UI_SCALE") == "auto"
    assert (
        app._effective_env_default(
            "CAVEVIEWER_AUTO_DIVE_RENDER_DISTANCE_CELLS"
        )
        == "10"
    )
    assert (
        app._effective_env_default(
            "CAVEVIEWER_AUTO_DIVE_SMOOTHING_RADIUS_CELLS"
        )
        == "5"
    )
    assert app._effective_env_default("CAVEVIEWER_AUTO_DIVE_DIAGNOSTICS") == "0"
