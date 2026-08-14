"""Pure, platform-aware keyboard-control catalog shared by CaveViewer UIs."""

from __future__ import annotations

from dataclasses import dataclass

from caveviewer.gui.platform.presentation import (
    PresentationProfile,
    get_presentation_profile,
)


@dataclass(frozen=True, slots=True)
class KeyboardShortcut:
    """One user-visible keyboard command and any context needed to use it."""

    id: str
    shortcut: str
    action: str
    context_note: str | None = None


@dataclass(frozen=True, slots=True)
class KeyboardShortcutSection:
    """A stable, ordered heading and its keyboard commands."""

    id: str
    title: str
    shortcuts: tuple[KeyboardShortcut, ...]


def _modifier_display_label(modifier_name: str) -> str:
    """Return the user-facing label for a profile-owned modifier name."""
    normalized = str(modifier_name or "").strip().lower()
    if normalized == "command":
        return "Cmd"
    if normalized == "control":
        return "Ctrl"
    return str(modifier_name)


def keyboard_control_sections(
    presentation_profile: PresentationProfile | None = None,
    *,
    include_main_window: bool = True,
) -> tuple[KeyboardShortcutSection, ...]:
    """Return the complete keyboard reference for one presentation profile.

    The main-window Help page requests all sections.  The OpenGL controls
    overlay requests only viewer controls and supplies its own mouse and
    button rows, which are intentionally outside the keyboard catalog.
    """
    profile = presentation_profile or get_presentation_profile()
    primary = profile.primary_shortcut_modifier_label
    bookmark_modifier = _modifier_display_label(profile.bookmark_save_modifier)
    bookmark_note = None
    if profile.shift_digit_bookmark_save_fallback:
        bookmark_note = (
            "Shift + digit is also accepted when the backend does not report Cmd."
        )

    main_window = KeyboardShortcutSection(
        id="main-window",
        title="Main window",
        shortcuts=(
            KeyboardShortcut(
                "main-window-focus",
                "Tab / Shift + Tab",
                "Move keyboard focus",
            ),
            KeyboardShortcut(
                "main-window-activate",
                "Return / Space",
                "Activate the focused control",
            ),
            KeyboardShortcut(
                "main-window-open-local-map",
                "Return",
                "Open a local map",
                "When Map Library is active and no focused control consumes it.",
            ),
            KeyboardShortcut(
                "main-window-back-or-close",
                f"Escape / {primary} + W",
                "Return, cancel, or close",
                "Help and About return to Map Library; Preferences keeps its discard check.",
            ),
        ),
    )
    move = KeyboardShortcutSection(
        id="move",
        title="Move",
        shortcuts=(
            KeyboardShortcut("move-strafe", "W A S D", "Move / strafe"),
            KeyboardShortcut("move-vertical", "E / Q", "Move up / down"),
            KeyboardShortcut("move-speed-boost", "Shift", "Speed boost"),
            KeyboardShortcut("move-speed-decrease", "-", "Decrease fly speed"),
            KeyboardShortcut("move-speed-increase", "=", "Increase fly speed"),
        ),
    )
    look = KeyboardShortcutSection(
        id="look",
        title="Look",
        shortcuts=(
            KeyboardShortcut("look-arrows", "Arrow keys", "Look around"),
            KeyboardShortcut("look-jlik", "J L I K", "Look around"),
            KeyboardShortcut("look-roll", "Z X", "Barrel roll"),
            KeyboardShortcut(
                "view-reset",
                f"{primary} + 0",
                "Reset view (level horizon)",
            ),
        ),
    )
    navigate = KeyboardShortcutSection(
        id="navigate",
        title="Navigate",
        shortcuts=(
            KeyboardShortcut(
                "bookmark-save",
                f"{bookmark_modifier} + 1..9",
                "Save camera bookmark slot",
                bookmark_note,
            ),
            KeyboardShortcut(
                "bookmark-recall",
                "1..9",
                "Recall camera bookmark slot",
            ),
            KeyboardShortcut(
                "bookmark-delete",
                "Del + 1..9",
                "Delete bookmark slot",
            ),
            KeyboardShortcut(
                "map-open",
                f"{primary} + O",
                "Switch to a different map",
            ),
            KeyboardShortcut(
                "recorded-dive-space",
                "Space",
                "Pause/resume a recorded dive",
                "Begins exploration after the startup controls screen is ready.",
            ),
            KeyboardShortcut(
                "viewer-escape",
                "Esc",
                "Close window",
                "Cancels a user-owned slice before publication.",
            ),
        ),
    )
    capture = KeyboardShortcutSection(
        id="capture",
        title="Capture",
        shortcuts=(
            KeyboardShortcut(
                "recording-toggle",
                f"{primary} + R",
                "Start/stop recording",
                "A second press during the countdown cancels it.",
            ),
            KeyboardShortcut(
                "manual-trace-toggle",
                f"{primary} + T",
                "Start/stop manual trace",
                "A second press during the countdown cancels it.",
            ),
            KeyboardShortcut(
                "slice-toggle",
                f"{primary} + C",
                "Start/stop slice",
                "A second press during the countdown cancels it; once active, it finishes and saves the slice.",
            ),
            KeyboardShortcut(
                "import-pause",
                f"{primary} + Shift + P",
                "Pause active map import",
            ),
        ),
    )

    sections = (move, look, navigate, capture)
    if include_main_window:
        return (main_window, *sections)
    return sections
