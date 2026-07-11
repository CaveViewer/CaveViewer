"""Check shared Tk theme consistency and immutability."""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from caveviewer.gui import advanced_settings_dialog, sample_maps_dialog, splash_screen
from caveviewer.gui.tk_theme import DARK_THEME


def test_dialogs_share_the_same_theme_tokens():
    for dialog_module in (
        splash_screen,
        advanced_settings_dialog,
        sample_maps_dialog,
    ):
        assert dialog_module._BG_COLOR == DARK_THEME.background
        assert dialog_module._TITLE_COLOR == DARK_THEME.title
        assert dialog_module._BUTTON_BG == DARK_THEME.primary_button
        assert dialog_module._BUTTON_BORDER_COLOR == DARK_THEME.primary_button_border


def test_theme_tokens_are_immutable():
    with pytest.raises(FrozenInstanceError):
        DARK_THEME.background = "#ffffff"
