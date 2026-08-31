"""Pure, platform-aware viewer-key catalog shared by CaveViewer help UIs."""

from __future__ import annotations

from dataclasses import dataclass

from caveviewer.gui.platform.presentation import (
    PresentationProfile,
    get_presentation_profile,
)


@dataclass(frozen=True, slots=True)
class KeyboardShortcut:
    """One direct viewer key binding and its concise user-facing action."""

    id: str
    shortcut: str
    action: str
    context_note: str | None = None


@dataclass(frozen=True, slots=True)
class KeyboardShortcutSection:
    """A stable, ordered group of related direct viewer key bindings."""

    id: str
    title: str
    shortcuts: tuple[KeyboardShortcut, ...]


# These chords remain available in the viewer but are intentionally omitted
# from both Help presentations. The import-pause chord is an undocumented
# Easter egg, while the bookmark chord is a compact alternate binding.
_HELP_HIDDEN_SHORTCUT_IDS = frozenset(
    {"bookmark-delete-control-shift", "import-pause"}
)
_SPACED_KEY_GROUPS = frozenset({"W A S D", "E Q", "J L I K", "Z X", "- ="})


def is_help_shortcut_visible(shortcut: KeyboardShortcut) -> bool:
    """Return whether a supported shortcut belongs in either Help UI."""
    return shortcut.id not in _HELP_HIDDEN_SHORTCUT_IDS


def _modifier_display_label(modifier_name: str) -> str:
    """Return the user-facing label for a profile-owned modifier name."""
    normalized = str(modifier_name or "").strip().lower()
    if normalized == "command":
        return "Cmd"
    if normalized == "control":
        return "Ctrl"
    return str(modifier_name)


def shortcut_keycap_parts(shortcut: str) -> tuple[str, ...]:
    """Split one shortcut into keycap text and non-keycap separators.

    The result is presentation-neutral: a Tk table can render the non-keycap
    separators as text while the OpenGL overlay can draw the remaining parts
    with its existing keycap primitive.
    """
    label = str(shortcut or "").strip()
    if not label:
        return ()
    if " + " in label:
        parts: list[str] = []
        for index, part in enumerate(label.split(" + ")):
            if index:
                parts.append("+")
            parts.append(part)
        return tuple(parts)
    if " / " in label:
        left, right = label.split(" / ", maxsplit=1)
        return (left, "/", right)
    if label in _SPACED_KEY_GROUPS:
        return tuple(label.split())
    return (label,)


def keyboard_control_sections(
    presentation_profile: PresentationProfile | None = None,
) -> tuple[KeyboardShortcutSection, ...]:
    """Return every direct viewer key binding for one presentation profile.

    The catalog intentionally excludes splash-window focus and navigation
    keys. Their result depends on the focused widget and active surface, so
    presenting them as one universal key binding would be misleading.
    """
    profile = presentation_profile or get_presentation_profile()
    primary = profile.primary_shortcut_modifier_label
    bookmark_modifier = _modifier_display_label(profile.bookmark_save_modifier)

    bookmark_rows = [
        KeyboardShortcut(
            "bookmark-save",
            f"{bookmark_modifier} + 1–9",
            "Save camera bookmark",
        ),
        KeyboardShortcut(
            "bookmark-recall",
            "1–9",
            "Recall camera bookmark",
        ),
        KeyboardShortcut(
            "bookmark-delete",
            "Delete + 1–9",
            "Delete camera bookmark",
        ),
        KeyboardShortcut(
            "bookmark-delete-control-shift",
            "Ctrl + Shift + 1–9",
            "Delete camera bookmark",
        ),
    ]
    if profile.shift_digit_bookmark_save_fallback:
        bookmark_rows.insert(
            1,
            KeyboardShortcut(
                "bookmark-save-shift-fallback",
                "Shift + 1–9",
                "Save camera bookmark",
            ),
        )

    return (
        KeyboardShortcutSection(
            id="movement",
            title="Movement",
            shortcuts=(
                KeyboardShortcut(
                    "move-strafe",
                    "W A S D",
                    "Move forward, left, backward, and right",
                ),
                KeyboardShortcut("move-vertical", "E Q", "Move up / down"),
                KeyboardShortcut("move-speed-boost", "Shift", "Speed boost"),
                KeyboardShortcut(
                    "move-speed-decrease",
                    "-",
                    "Decrease fly speed",
                ),
                KeyboardShortcut(
                    "move-speed-increase",
                    "=",
                    "Increase fly speed",
                ),
            ),
        ),
        KeyboardShortcutSection(
            id="view",
            title="View",
            shortcuts=(
                KeyboardShortcut(
                    "look-arrows",
                    "Arrow keys",
                    "Look left, right, up, and down",
                ),
                KeyboardShortcut(
                    "look-jlik",
                    "J L I K",
                    "Look left, right, up, and down",
                ),
                KeyboardShortcut("look-roll", "Z X", "Roll left / right"),
                KeyboardShortcut(
                    "view-reset",
                    f"{primary} + 0",
                    "Reset view (level horizon)",
                ),
            ),
        ),
        KeyboardShortcutSection(
            id="bookmarks",
            title="Bookmarks",
            shortcuts=tuple(bookmark_rows),
        ),
        KeyboardShortcutSection(
            id="map",
            title="Map",
            shortcuts=(
                KeyboardShortcut(
                    "map-open",
                    f"{primary} + O",
                    "Open another map",
                ),
            ),
        ),
        KeyboardShortcutSection(
            id="map-import",
            title="Map Import",
            shortcuts=(
                KeyboardShortcut(
                    "import-pause",
                    f"{primary} + Shift + P",
                    "Pause active import",
                ),
            ),
        ),
        KeyboardShortcutSection(
            id="recorded-dive",
            title="Recorded Dive",
            shortcuts=(
                KeyboardShortcut(
                    "recorded-dive-space",
                    "Space",
                    "Pause/resume recorded dive",
                ),
            ),
        ),
        KeyboardShortcutSection(
            id="capture",
            title="Capture",
            shortcuts=(
                KeyboardShortcut(
                    "recording-toggle",
                    f"{primary} + R",
                    "Start/stop recording",
                ),
                KeyboardShortcut(
                    "manual-trace-toggle",
                    f"{primary} + T",
                    "Start/stop manual trace",
                ),
                KeyboardShortcut(
                    "slice-toggle",
                    f"{primary} + C",
                    "Start/stop slice",
                ),
                KeyboardShortcut(
                    "capture-cancel",
                    "Escape",
                    "Cancel active capture",
                ),
            ),
        ),
    )
