"""Scale-once visual and content contract for the embedded Help surface."""

from __future__ import annotations

from dataclasses import dataclass, fields
from typing import Callable

from caveviewer.gui.preferences_style import PREFERENCES_VISUAL_METRICS
from caveviewer.gui.tk_theme import DARK_THEME, TkTheme


@dataclass(frozen=True, slots=True)
class ScaledHelpVisualMetrics:
    """Help geometry after one logical-to-display conversion."""

    control_corner_radius: int
    section_corner_radius: int
    section_padding_x: int
    section_padding_y: int
    section_gap_y: int
    section_heading_to_description_y: int
    section_description_to_content_y: int
    card_item_gap_y: int
    control_height: int
    action_min_width: int
    control_content_pad_x: int
    control_border_thickness: int
    focus_border_thickness: int
    content_min_width: int
    content_right_pad_x: int
    keycap_lane_min_width: int
    keycap_lane_max_width: int
    action_lane_gap_x: int
    shortcut_row_pad_y: int
    keycap_content_pad_y: int
    keycap_sequence_gap_x: int
    detail_gap_y: int
    error_excerpt_padding: int
    content_bottom_pad_y: int


@dataclass(frozen=True, slots=True)
class HelpVisualMetrics:
    """Unscaled geometry shared by every Help tab renderer."""

    control_corner_radius: int = PREFERENCES_VISUAL_METRICS.control_corner_radius
    section_corner_radius: int = PREFERENCES_VISUAL_METRICS.section_corner_radius
    section_padding_x: int = PREFERENCES_VISUAL_METRICS.section_padding_x
    section_padding_y: int = PREFERENCES_VISUAL_METRICS.section_padding_y
    section_gap_y: int = PREFERENCES_VISUAL_METRICS.section_gap_y
    section_heading_to_description_y: int = (
        PREFERENCES_VISUAL_METRICS.section_heading_to_description_y
    )
    section_description_to_content_y: int = (
        PREFERENCES_VISUAL_METRICS.section_description_to_fields_y
    )
    card_item_gap_y: int = PREFERENCES_VISUAL_METRICS.card_item_gap_y
    control_height: int = PREFERENCES_VISUAL_METRICS.control_height
    action_min_width: int = PREFERENCES_VISUAL_METRICS.action_min_width
    control_content_pad_x: int = PREFERENCES_VISUAL_METRICS.control_content_pad_x
    control_border_thickness: int = (
        PREFERENCES_VISUAL_METRICS.control_border_thickness
    )
    focus_border_thickness: int = PREFERENCES_VISUAL_METRICS.focus_border_thickness
    content_min_width: int = 320
    content_right_pad_x: int = 12
    keycap_lane_min_width: int = 170
    keycap_lane_max_width: int = 250
    action_lane_gap_x: int = 32
    shortcut_row_pad_y: int = 7
    keycap_content_pad_y: int = 2
    keycap_sequence_gap_x: int = 5
    detail_gap_y: int = 3
    error_excerpt_padding: int = 12
    content_bottom_pad_y: int = 16

    def scaled(
        self,
        px: Callable[[int | float], int],
    ) -> ScaledHelpVisualMetrics:
        """Convert every logical metric exactly once for one Help surface."""

        values = {field.name: px(getattr(self, field.name)) for field in fields(self)}
        return ScaledHelpVisualMetrics(**values)


@dataclass(frozen=True, slots=True)
class HelpVisualPalette:
    """Semantic colors used by Help tabs, cards, keycaps, and feedback."""

    surface_background: str
    section_background: str
    heading_text: str
    supporting_text: str
    keycap_background: str
    keycap_border: str
    keycap_text: str
    action_text: str
    tab_active_text: str
    tab_inactive_text: str
    control_focus_border: str
    error_text: str
    primary_action_background: str
    primary_action_hover_background: str
    primary_action_pressed_background: str
    primary_action_border: str
    primary_action_text: str
    secondary_action_background: str
    secondary_action_hover_background: str
    secondary_action_pressed_background: str
    secondary_action_border: str
    secondary_action_text: str
    disabled_action_background: str
    disabled_action_border: str
    disabled_action_text: str


@dataclass(frozen=True, slots=True)
class HelpTypographyRoles:
    """Names of the shared Tk typography roles used by Help."""

    active_tab: str = "body_strong"
    inactive_tab: str = "body"
    section_heading: str = "heading"
    section_description: str = "supporting"
    keycap: str = "body_strong"
    action: str = "body"
    action_strong: str = "body_strong"
    detail: str = "supporting"
    error: str = "supporting"


@dataclass(frozen=True, slots=True)
class HelpSectionContent:
    """Stable title and purpose text for one Help card."""

    tab_key: str
    section_id: str
    title: str
    purpose: str


def help_visual_palette(theme: TkTheme = DARK_THEME) -> HelpVisualPalette:
    """Resolve the Help palette through the active semantic Tk theme."""

    return HelpVisualPalette(
        surface_background=theme.background,
        section_background=theme.panel,
        heading_text=theme.body_text,
        supporting_text=theme.secondary_text,
        keycap_background=theme.entry_background,
        keycap_border=theme.secondary_button_border,
        keycap_text=theme.body_text,
        action_text=theme.body_text,
        tab_active_text=theme.primary_button,
        tab_inactive_text=theme.secondary_text,
        control_focus_border=theme.entry_focus_border,
        error_text=theme.error_text,
        primary_action_background=theme.primary_button,
        primary_action_hover_background=theme.primary_button_hover,
        primary_action_pressed_background=theme.primary_button_border,
        primary_action_border=theme.primary_button_border,
        primary_action_text=theme.primary_button_text,
        secondary_action_background=theme.secondary_button,
        secondary_action_hover_background=theme.secondary_button_hover,
        secondary_action_pressed_background=theme.secondary_button_border,
        secondary_action_border=theme.secondary_button_border,
        secondary_action_text=theme.body_text,
        disabled_action_background=theme.secondary_button,
        disabled_action_border=theme.entry_border,
        disabled_action_text=theme.placeholder_text,
    )


HELP_SECTION_CONTENT = (
    HelpSectionContent(
        "keys", "move", "Move", "Control movement direction and speed."
    ),
    HelpSectionContent(
        "keys",
        "look",
        "Look",
        "Control the direction and orientation of the view.",
    ),
    HelpSectionContent(
        "keys",
        "navigate",
        "Navigate",
        "Open maps and use saved locations or routes.",
    ),
    HelpSectionContent(
        "capture",
        "capture-control",
        "Capture Control",
        "Manage a capture already in progress.",
    ),
    HelpSectionContent(
        "capture", "video", "Video", "Record what you see as a video."
    ),
    HelpSectionContent(
        "capture",
        "dive-trace",
        "Dive Trace",
        "Save camera movement for replay or analysis.",
    ),
    HelpSectionContent(
        "capture",
        "cave-slice",
        "Cave Slice",
        "Save part of a cave as a new pre-compiled map.",
    ),
    HelpSectionContent(
        "troubleshooting",
        "application-logs",
        "Application Logs",
        "Open the latest log when you need help diagnosing a problem.",
    ),
    HelpSectionContent(
        "troubleshooting",
        "last-error",
        "Last Error",
        "Review and copy details from the latest recorded error.",
    ),
)


def help_section_content(tab_key: str, section_id: str) -> HelpSectionContent:
    """Return the declared content for one Help card."""

    for content in HELP_SECTION_CONTENT:
        if content.tab_key == tab_key and content.section_id == section_id:
            return content
    raise KeyError((tab_key, section_id))


HELP_VISUAL_METRICS = HelpVisualMetrics()
HELP_VISUAL_PALETTE = help_visual_palette()
HELP_TYPOGRAPHY_ROLES = HelpTypographyRoles()
