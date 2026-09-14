"""Verify the shared semantic typography scale for Tk presentation surfaces."""

from __future__ import annotations

from caveviewer.gui.platform.presentation import select_presentation_profile
from caveviewer.gui.tk_typography import create_tk_typography, resolve_tk_text_scale


def test_semantic_typography_roles_use_the_documented_base_scale():
    typography = create_tk_typography("CaveViewer Sans")

    assert typography.display == ("CaveViewer Sans", -24, "bold")
    assert typography.heading == ("CaveViewer Sans", -19, "bold")
    assert typography.body_strong == ("CaveViewer Sans", -13, "bold")
    assert typography.body == ("CaveViewer Sans", -13)
    assert typography.supporting == ("CaveViewer Sans", -12)
    assert typography.section == ("CaveViewer Sans", -12, "bold")
    assert typography.medium_family == "CaveViewer Sans"
    assert typography.medium_styles == ()
    assert typography.semibold_family == "CaveViewer Sans"
    assert typography.semibold_styles == ("bold",)
    assert typography.text_scale == 1.0
    assert typography.display_scale == 1.0


def test_semantic_typography_applies_accessibility_scale_once():
    typography = create_tk_typography("CaveViewer Sans", text_scale=1.4)

    assert typography.display == ("CaveViewer Sans", -34, "bold")
    assert typography.heading == ("CaveViewer Sans", -27, "bold")
    assert typography.body_strong == ("CaveViewer Sans", -18, "bold")
    assert typography.body == ("CaveViewer Sans", -18)
    assert typography.supporting == ("CaveViewer Sans", -17)
    assert typography.section == ("CaveViewer Sans", -17, "bold")
    assert typography.text_scale == 1.4


def test_semantic_typography_applies_display_scale_once():
    typography = create_tk_typography("CaveViewer Sans", display_scale=2.0)

    assert typography.display == ("CaveViewer Sans", -48, "bold")
    assert typography.heading == ("CaveViewer Sans", -38, "bold")
    assert typography.body_strong == ("CaveViewer Sans", -26, "bold")
    assert typography.body == ("CaveViewer Sans", -26)
    assert typography.supporting == ("CaveViewer Sans", -24)
    assert typography.section == ("CaveViewer Sans", -24, "bold")
    assert typography.display_scale == 2.0


def test_resolve_tk_text_scale_reads_accessibility_size_from_tk(monkeypatch):
    class DefaultFont:
        @staticmethod
        def actual(option):
            assert option == "size"
            return 15

    root = object()
    calls = []
    monkeypatch.setattr(
        "tkinter.font.nametofont",
        lambda name, *, root: calls.append((name, root)) or DefaultFont(),
    )

    profile = select_presentation_profile(platform_name="darwin")

    assert resolve_tk_text_scale(root, profile) == 1.25
    assert calls == [("TkDefaultFont", root)]


def test_resolve_tk_text_scale_skips_desktop_default_when_profile_disables_it(
    monkeypatch,
):
    monkeypatch.setattr(
        "tkinter.font.nametofont",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("unexpected")),
    )

    profile = select_presentation_profile(platform_name="linux")

    assert resolve_tk_text_scale(object(), profile) == 1.0


def test_semantic_typography_accepts_a_registered_semibold_face():
    typography = create_tk_typography(
        "Inter",
        semibold_family="Inter SemiBold",
        semibold_styles=(),
    )

    assert typography.semibold_family == "Inter SemiBold"
    assert typography.semibold_styles == ()


def test_semantic_typography_accepts_a_registered_medium_face():
    typography = create_tk_typography(
        "Inter",
        medium_family="Inter Medium",
        medium_styles=(),
    )

    assert typography.medium_family == "Inter Medium"
    assert typography.medium_styles == ()
