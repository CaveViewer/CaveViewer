"""Shared visual tokens for CaveViewer's Tk dialogs."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TkTheme:
    background: str
    panel: str
    title: str
    body_text: str
    secondary_text: str
    primary_button: str
    primary_button_hover: str
    primary_button_border: str
    primary_button_text: str
    secondary_button: str
    secondary_button_hover: str
    secondary_button_border: str
    border: str
    entry_background: str
    entry_border: str
    entry_focus_border: str
    invalid_border: str
    placeholder_text: str
    error_text: str


DARK_THEME = TkTheme(
    background="#0a0a0d",
    panel="#12121a",
    title="#f2d98c",
    body_text="#cccdd6",
    secondary_text="#9a9aa6",
    primary_button="#e5a11f",
    primary_button_hover="#f0b13a",
    primary_button_border="#9c6f18",
    primary_button_text="#1a1408",
    secondary_button="#2a2a33",
    secondary_button_hover="#33333f",
    secondary_button_border="#3a4454",
    border="#5c5c6e",
    entry_background="#1c1c24",
    entry_border="#30303a",
    entry_focus_border="#5d6f8a",
    invalid_border="#ff6b6b",
    placeholder_text="#747481",
    error_text="#ff9b90",
)
