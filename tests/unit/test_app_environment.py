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
    assert app._effective_env_default("CAVEVIEWER_TEXT_AA_MODE") == "normal"


def test_tk_scale_is_a_known_runtime_setting():
    assert "CAVEVIEWER_TK_SCALE" in app._KNOWN_CAVEVIEWER_ENV_VARS
