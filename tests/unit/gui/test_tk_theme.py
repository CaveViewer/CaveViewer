"""Check shared Tk theme consistency and immutability."""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from caveviewer.gui import (
    preferences_dialog,
    dialog_style,
    splash_screen,
)
from caveviewer.gui.help_style import HELP_VISUAL_PALETTE
from caveviewer.gui.map_library_style import (
    MAP_LIBRARY_PANEL_FILL,
    MAP_LIBRARY_WINDOW_FILL,
)
from caveviewer.gui.preferences_style import PREFERENCES_PRIMARY_TEXT
from caveviewer.gui.tk_theme import (
    APPLICATION_PANEL_FILL,
    APPLICATION_SURFACE_FILL,
    DARK_THEME,
)


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
    assert palette.heading_text == PREFERENCES_PRIMARY_TEXT
    assert palette.primary_action_background == DARK_THEME.primary_button
    assert palette.primary_action_border == DARK_THEME.primary_button_border
    assert dialog_style.DIALOG_PANEL_BORDER == DARK_THEME.entry_border


def test_embedded_views_share_the_map_library_surface_palette():
    preferences = preferences_dialog.PREFERENCES_VISUAL_PALETTE

    assert APPLICATION_SURFACE_FILL == MAP_LIBRARY_WINDOW_FILL == "#0D0F13"
    assert APPLICATION_PANEL_FILL == MAP_LIBRARY_PANEL_FILL == "#15171C"
    assert DARK_THEME.background == APPLICATION_SURFACE_FILL
    assert DARK_THEME.panel == APPLICATION_PANEL_FILL
    assert preferences.surface_background == APPLICATION_SURFACE_FILL
    assert preferences.section_background == APPLICATION_PANEL_FILL
    assert HELP_VISUAL_PALETTE.surface_background == APPLICATION_SURFACE_FILL
    assert HELP_VISUAL_PALETTE.section_background == APPLICATION_PANEL_FILL
    assert splash_screen._BG_COLOR == APPLICATION_SURFACE_FILL
    assert splash_screen._PANEL_COLOR == APPLICATION_PANEL_FILL


def test_theme_tokens_are_immutable():
    with pytest.raises(FrozenInstanceError):
        DARK_THEME.background = "#ffffff"
