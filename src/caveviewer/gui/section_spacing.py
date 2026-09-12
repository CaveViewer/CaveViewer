"""Logical spacing shared by named content groups in CaveViewer panels."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ContentSectionSpacing:
    """Logical distances around a section heading and its following content."""

    heading_to_content_y: int = 13
    between_sections_y: int = 26


STANDARD_CONTENT_SECTION_SPACING = ContentSectionSpacing()
PRIMARY_LABEL_ROW_TOP_INSET = 16
PRIMARY_LABEL_ROW_HEIGHT = 44
PRIMARY_SURFACE_VERTICAL_MARGIN = 14
