"""Check shared Tk theme consistency and immutability."""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from caveviewer.gui import (
    preferences_dialog,
    dialog_style,
    splash_screen,
)
from caveviewer.gui.tk_theme import DARK_THEME


def test_dialogs_share_the_same_theme_tokens():
    assert splash_screen._BG_COLOR == DARK_THEME.background
    assert splash_screen._TITLE_COLOR == DARK_THEME.title
    assert splash_screen._BUTTON_BG == DARK_THEME.primary_button
    assert (
        splash_screen._BUTTON_BORDER_COLOR == DARK_THEME.primary_button_border
    )

    assert preferences_dialog._BG_COLOR == DARK_THEME.background
    palette = preferences_dialog.PREFERENCES_VISUAL_PALETTE
    assert palette.surface_background == DARK_THEME.background
    assert palette.heading_text == DARK_THEME.body_text
    assert palette.primary_action_background == DARK_THEME.primary_button
    assert palette.primary_action_border == DARK_THEME.primary_button_border
    assert dialog_style.DIALOG_PANEL_BORDER == DARK_THEME.entry_border


def test_theme_tokens_are_immutable():
    with pytest.raises(FrozenInstanceError):
        DARK_THEME.background = "#ffffff"
