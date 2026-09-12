"""Scale-once visual contract for the splash Map Library surface."""

from __future__ import annotations

from dataclasses import dataclass, fields
from typing import Callable

from caveviewer.gui.preferences_style import PREFERENCES_VISUAL_METRICS
from caveviewer.gui.tk_feedback import ERROR_FEEDBACK_MS, SUCCESS_FEEDBACK_MS
from caveviewer.gui.tk_theme import (
    APPLICATION_PANEL_FILL,
    APPLICATION_SURFACE_FILL,
    DARK_THEME,
    TkTheme,
)
from caveviewer.gui.tk_typography import TkTypography


OPEN_LOCAL_MAP_TITLE = "Open a local map"
OPEN_LOCAL_MAP_DESCRIPTION = "Browse a cave map folder on your computer."
OPEN_ANOTHER_LOCAL_MAP_TITLE = "Open another local map"
MAP_LIBRARY_WINDOW_FILL = APPLICATION_SURFACE_FILL
MAP_LIBRARY_PANEL_FILL = APPLICATION_PANEL_FILL
MAP_LIBRARY_MAIN_LEFT_GAP = 26
MAP_LIBRARY_RIGHT_MARGIN = 28
MAP_LIBRARY_SCROLLBAR_RAIL_WIDTH = 14
MAP_LIBRARY_RECENT_CARD_BORDER = "#30343D"
MAP_LIBRARY_CATALOG_CARD_BORDER = "#313337"
MAP_LIBRARY_PRIMARY_FG = "#F5C451"
MAP_LIBRARY_TITLE_FG = "#EFF1F5"
MAP_LIBRARY_DESCRIPTION_FG = "#B6BCC8"
MAP_LIBRARY_MUTED_FG = "#A9AFBC"
SECTION_HEADING_FONT_SCALE = 0.8


def _scaled_font(font: tuple, factor: float) -> tuple:
    """Scale one resolved Tk font tuple while preserving its family and styles."""
    if len(font) < 2:
        return font
    values = list(font)
    values[1] = max(1, int(round(float(values[1]) * factor)))
    return tuple(values)


def _logical_pixel_font(
    font: tuple,
    *,
    px: Callable[[int | float], int],
    size: int,
    semibold_family: str | None = None,
    semibold_styles: tuple[str, ...] = (),
) -> tuple:
    """Resolve an exact logical-pixel font with a platform-safe emphasis."""
    return (
        semibold_family or font[0],
        -max(1, px(size)),
        *semibold_styles,
    )


@dataclass(frozen=True, slots=True)
class ScaledMapLibraryVisualMetrics:
    """Map Library geometry after one logical-to-display conversion."""

    surface_inset_x: int
    surface_top_pad_y: int
    surface_bottom_pad_y: int
    section_corner_radius: int
    section_border_thickness: int
    section_padding_x: int
    section_padding_y: int
    section_padding_bottom_y: int
    section_gap_y: int
    recent_card_min_height: int
    catalog_card_min_height: int
    section_header_height: int
    section_header_to_body_y: int
    local_action_height: int
    local_action_icon_width: int
    local_action_icon_height: int
    local_action_icon_to_text_x: int
    compact_local_action_height: int
    compact_local_action_icon_width: int
    compact_local_action_icon_height: int
    compact_local_action_icon_to_text_x: int
    recent_rows_to_local_action_y: int
    row_gap_y: int
    row_content_pad_y: int
    row_text_end_pad_x: int
    metadata_top_gap_y: int
    size_leading_pad_x: int
    size_trailing_pad_x: int
    action_gap_x: int
    overflow_trailing_pad_x: int
    progress_height: int
    progress_top_gap_y: int
    action_retry_icon_diameter: int
    action_stop_size: int
    action_button_size: int
    action_icon_stroke_width: int
    overflow_button_size: int
    control_corner_radius: int
    control_border_thickness: int
    focus_border_thickness: int


@dataclass(frozen=True, slots=True)
class MapLibraryVisualMetrics:
    """Unscaled geometry shared by every Map Library state."""

    surface_inset_x: int = 0
    surface_top_pad_y: int = 44
    surface_bottom_pad_y: int = 30
    section_corner_radius: int = 10
    section_border_thickness: int = 1
    section_padding_x: int = 24
    section_padding_y: int = 20
    section_padding_bottom_y: int = 25
    section_gap_y: int = 24
    recent_card_min_height: int = 138
    catalog_card_min_height: int = 484
    section_header_height: int = 22
    section_header_to_body_y: int = PREFERENCES_VISUAL_METRICS.card_item_gap_y
    local_action_height: int = 50
    local_action_icon_width: int = 32
    local_action_icon_height: int = 32
    local_action_icon_to_text_x: int = 14
    compact_local_action_height: int = 28
    compact_local_action_icon_width: int = 20
    compact_local_action_icon_height: int = 20
    compact_local_action_icon_to_text_x: int = 12
    recent_rows_to_local_action_y: int = 12
    row_gap_y: int = 8
    row_content_pad_y: int = 4
    row_text_end_pad_x: int = 8
    metadata_top_gap_y: int = 2
    size_leading_pad_x: int = 8
    size_trailing_pad_x: int = 12
    action_gap_x: int = 4
    overflow_trailing_pad_x: int = 0
    progress_height: int = 3
    progress_top_gap_y: int = 5
    action_retry_icon_diameter: int = 16
    action_stop_size: int = 6
    action_button_size: int = 28
    action_icon_stroke_width: int = 2
    overflow_button_size: int = 24
    control_corner_radius: int = PREFERENCES_VISUAL_METRICS.control_corner_radius
    control_border_thickness: int = (
        PREFERENCES_VISUAL_METRICS.control_border_thickness
    )
    focus_border_thickness: int = PREFERENCES_VISUAL_METRICS.focus_border_thickness

    def scaled(
        self,
        px: Callable[[int | float], int],
    ) -> ScaledMapLibraryVisualMetrics:
        """Convert every logical metric exactly once for one composed surface."""

        values = {field.name: px(getattr(self, field.name)) for field in fields(self)}
        return ScaledMapLibraryVisualMetrics(**values)


@dataclass(frozen=True, slots=True)
class MapLibraryVisualPalette:
    """Semantic colors used by Map Library cards, rows, and controls."""

    surface_background: str
    section_background: str
    section_border: str
    catalog_section_border: str
    section_heading_text: str
    disclosure_text: str
    map_title_text: str
    former_map_title_text: str
    local_action_text: str
    local_action_supporting_text: str
    supporting_text: str
    file_size_text: str
    status_text: str
    error_text: str
    action_background: str
    action_foreground: str
    action_hover_background: str
    action_pressed_background: str
    action_focus_border: str
    disabled_action_background: str
    disabled_action_foreground: str
    disabled_action_border: str
    overflow_foreground: str
    overflow_hover_foreground: str
    overflow_hover_background: str
    menu_background: str
    menu_border: str
    menu_hover_background: str
    menu_text: str


@dataclass(frozen=True, slots=True)
class MapLibraryTypographyRoles:
    """Names of the shared Tk typography roles used by Map Library."""

    section_heading: str = "heading"
    map_title: str = "body_strong"
    local_action_title: str = "body_strong"
    compact_local_action: str = "body"
    body: str = "body"
    supporting: str = "supporting"


@dataclass(frozen=True, slots=True)
class MapLibraryPanelStyle:
    """Resolved theme, typography, feedback, and geometry for one panel."""

    metrics: ScaledMapLibraryVisualMetrics
    panel_color: str
    panel_border_color: str
    catalog_card_border_color: str
    card_color: str
    title_color: str
    local_action_title_color: str
    local_action_supporting_color: str
    former_map_title_color: str
    instruction_color: str
    disclosure_color: str
    title_font: tuple
    local_action_font: tuple
    body_font: tuple
    supporting_font: tuple
    section_font: tuple
    button_bg: str
    button_fg: str
    button_hover_bg: str
    featured_action_bg: str
    featured_action_hover_bg: str
    featured_action_pressed_bg: str
    button_border_color: str
    disabled_button_bg: str
    disabled_button_fg: str
    disabled_button_border: str
    empty_note_color: str
    metadata_color: str
    file_size_color: str
    metadata_error_color: str
    metadata_status_color: str
    metadata_status_duration_ms: int
    metadata_error_duration_ms: int
    progress_track_color: str
    progress_fill_color: str
    action_retry_icon_diameter: int
    action_stop_size: int
    action_button_size: int
    action_icon_stroke_width: int
    overflow_button_size: int
    overflow_fg: str
    overflow_hover_fg: str
    overflow_hover_bg: str
    menu_bg: str
    menu_border: str
    menu_hover_bg: str
    menu_text: str


def map_library_visual_palette(
    theme: TkTheme = DARK_THEME,
) -> MapLibraryVisualPalette:
    """Resolve the Map Library palette through the active semantic Tk theme."""

    return MapLibraryVisualPalette(
        surface_background=MAP_LIBRARY_WINDOW_FILL,
        section_background=MAP_LIBRARY_PANEL_FILL,
        section_border=MAP_LIBRARY_RECENT_CARD_BORDER,
        catalog_section_border=MAP_LIBRARY_CATALOG_CARD_BORDER,
        section_heading_text=theme.body_text,
        disclosure_text=MAP_LIBRARY_PRIMARY_FG,
        map_title_text=MAP_LIBRARY_TITLE_FG,
        former_map_title_text=theme.secondary_text,
        local_action_text=MAP_LIBRARY_PRIMARY_FG,
        local_action_supporting_text=MAP_LIBRARY_MUTED_FG,
        supporting_text=MAP_LIBRARY_DESCRIPTION_FG,
        file_size_text=MAP_LIBRARY_MUTED_FG,
        status_text=MAP_LIBRARY_MUTED_FG,
        error_text=theme.error_text,
        action_background=MAP_LIBRARY_PANEL_FILL,
        action_foreground=MAP_LIBRARY_PRIMARY_FG,
        action_hover_background=theme.secondary_button,
        action_pressed_background=theme.secondary_button_hover,
        action_focus_border=theme.entry_focus_border,
        disabled_action_background=MAP_LIBRARY_PANEL_FILL,
        disabled_action_foreground=theme.placeholder_text,
        disabled_action_border=theme.entry_border,
        overflow_foreground=MAP_LIBRARY_MUTED_FG,
        overflow_hover_foreground=theme.secondary_text,
        overflow_hover_background=theme.secondary_button,
        menu_background=theme.secondary_button,
        menu_border=theme.secondary_button_border,
        menu_hover_background=theme.secondary_button_hover,
        menu_text=theme.body_text,
    )


def create_map_library_panel_style(
    *,
    px: Callable[[int | float], int],
    typography: TkTypography,
    progress_track_color: str,
    progress_fill_color: str,
    theme: TkTheme = DARK_THEME,
) -> MapLibraryPanelStyle:
    """Compose one immutable panel style from shared semantic inputs."""

    metrics = MAP_LIBRARY_VISUAL_METRICS.scaled(px)
    palette = map_library_visual_palette(theme)
    return MapLibraryPanelStyle(
        metrics=metrics,
        panel_color=palette.surface_background,
        panel_border_color=palette.section_border,
        catalog_card_border_color=palette.catalog_section_border,
        card_color=palette.section_background,
        title_color=palette.map_title_text,
        local_action_title_color=palette.local_action_text,
        local_action_supporting_color=palette.local_action_supporting_text,
        former_map_title_color=palette.former_map_title_text,
        instruction_color=palette.section_heading_text,
        disclosure_color=palette.disclosure_text,
        title_font=_logical_pixel_font(
            typography.body_strong,
            px=px,
            size=14,
            semibold_family=typography.semibold_family,
            semibold_styles=typography.semibold_styles,
        ),
        local_action_font=_logical_pixel_font(
            typography.body,
            px=px,
            size=14,
        ),
        body_font=_logical_pixel_font(typography.body, px=px, size=12),
        supporting_font=_logical_pixel_font(
            typography.supporting,
            px=px,
            size=12,
        ),
        section_font=_scaled_font(typography.heading, SECTION_HEADING_FONT_SCALE),
        button_bg=palette.action_background,
        button_fg=palette.action_foreground,
        button_hover_bg=palette.action_hover_background,
        featured_action_bg=palette.section_background,
        featured_action_hover_bg=palette.action_hover_background,
        featured_action_pressed_bg=palette.action_pressed_background,
        button_border_color=palette.action_focus_border,
        disabled_button_bg=palette.disabled_action_background,
        disabled_button_fg=palette.disabled_action_foreground,
        disabled_button_border=palette.disabled_action_border,
        empty_note_color=palette.supporting_text,
        metadata_color=palette.supporting_text,
        file_size_color=palette.file_size_text,
        metadata_error_color=palette.error_text,
        metadata_status_color=palette.status_text,
        metadata_status_duration_ms=SUCCESS_FEEDBACK_MS,
        metadata_error_duration_ms=ERROR_FEEDBACK_MS,
        progress_track_color=progress_track_color,
        progress_fill_color=progress_fill_color,
        action_retry_icon_diameter=metrics.action_retry_icon_diameter,
        action_stop_size=metrics.action_stop_size,
        action_button_size=metrics.action_button_size,
        action_icon_stroke_width=metrics.action_icon_stroke_width,
        overflow_button_size=metrics.overflow_button_size,
        overflow_fg=palette.overflow_foreground,
        overflow_hover_fg=palette.overflow_hover_foreground,
        overflow_hover_bg=palette.overflow_hover_background,
        menu_bg=palette.menu_background,
        menu_border=palette.menu_border,
        menu_hover_bg=palette.menu_hover_background,
        menu_text=palette.menu_text,
    )


MAP_LIBRARY_VISUAL_METRICS = MapLibraryVisualMetrics()
MAP_LIBRARY_VISUAL_PALETTE = map_library_visual_palette()
MAP_LIBRARY_TYPOGRAPHY_ROLES = MapLibraryTypographyRoles()
