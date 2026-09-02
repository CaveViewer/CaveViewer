"""Verify the shared semantic typography scale for Tk presentation surfaces."""

from __future__ import annotations

from caveviewer.gui.tk_typography import create_tk_typography


def test_semantic_typography_roles_use_the_documented_base_scale():
    typography = create_tk_typography("CaveViewer Sans")

    assert typography.display == ("CaveViewer Sans", 18, "bold")
    assert typography.heading == ("CaveViewer Sans", 14, "bold")
    assert typography.body_strong == ("CaveViewer Sans", 10, "bold")
    assert typography.body == ("CaveViewer Sans", 10)
    assert typography.supporting == ("CaveViewer Sans", 9)
    assert typography.section == ("CaveViewer Sans", 9, "bold")


def test_semantic_typography_applies_accessibility_scale_once():
    typography = create_tk_typography("CaveViewer Sans", text_scale=1.4)

    assert typography.display == ("CaveViewer Sans", 25, "bold")
    assert typography.heading == ("CaveViewer Sans", 20, "bold")
    assert typography.body_strong == ("CaveViewer Sans", 14, "bold")
    assert typography.body == ("CaveViewer Sans", 14)
    assert typography.supporting == ("CaveViewer Sans", 13)
    assert typography.section == ("CaveViewer Sans", 13, "bold")
