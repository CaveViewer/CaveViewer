"""Logical spacing shared by named content groups in CaveViewer panels."""

from __future__ import annotations

from dataclasses import dataclass

from caveviewer.gui.tk_layout import FOOTER_GAP, PRIMARY_ROW_HEIGHT, SHELL_TOP_INSET


@dataclass(frozen=True, slots=True)
class ContentSectionSpacing:
    """Logical distances around a section heading and its following content."""

    heading_to_content_y: int = 12
    between_sections_y: int = 24


STANDARD_CONTENT_SECTION_SPACING = ContentSectionSpacing()
PRIMARY_LABEL_ROW_TOP_INSET = SHELL_TOP_INSET
PRIMARY_LABEL_ROW_HEIGHT = PRIMARY_ROW_HEIGHT
PRIMARY_LABEL_ROW_GAP = 8
PRIMARY_SURFACE_VERTICAL_MARGIN = FOOTER_GAP
