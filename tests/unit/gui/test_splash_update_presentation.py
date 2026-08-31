"""Verify the splash labels and actions for every visible update state."""

from __future__ import annotations

import ast
import inspect
import sys
import textwrap
from types import SimpleNamespace

import pytest

from caveviewer.gui import (
    cave_metadata_panel,
    dialog_style,
    help_panel,
    map_history,
    map_library_controller,
    map_library_panel,
    map_library_transfers,
    map_library_workflow,
    preferences_dialog,
    scrollable_content,
    splash_screen,
    top_tab_strip,
)
from caveviewer.gui.platform.presentation import select_presentation_profile
from caveviewer.gui.features import FeatureDecision, FeatureId, FeatureState
from caveviewer.gui.update_manager import UpdateSnapshot, UpdateState


@pytest.mark.parametrize(
    "module",
    (
        splash_screen,
        map_library_panel,
        cave_metadata_panel,
        preferences_dialog,
        help_panel,
        dialog_style,
        top_tab_strip,
        scrollable_content,
    ),
    ids=lambda module: module.__name__.rsplit(".", 1)[-1],
)
def test_splash_components_do_not_override_the_system_cursor(module):
    """Keep the OS cursor unchanged across every splash-owned surface."""
    tree = ast.parse(inspect.getsource(module))
    overrides = []
    for node in ast.walk(tree):
        if isinstance(node, ast.keyword) and node.arg == "cursor":
            overrides.append(node.lineno)
        elif isinstance(node, ast.Dict):
            overrides.extend(
                node.lineno
                for key in node.keys
                if isinstance(key, ast.Constant) and key.value == "cursor"
            )
        elif (
            isinstance(node, ast.Subscript)
            and isinstance(node.ctx, ast.Store)
            and isinstance(node.slice, ast.Constant)
            and node.slice.value == "cursor"
        ):
            overrides.append(node.lineno)

    assert overrides == []


def test_map_library_cave_details_stay_in_the_splash_content_area():
    splash_source = inspect.getsource(splash_screen.show_splash_screen)
    details_source = inspect.getsource(cave_metadata_panel.CaveMetadataPanel)

    assert "cave_metadata_surface = tk.Frame(right_frame, bg=_BG_COLOR)" in splash_source
    assert "show_cave_metadata=_show_cave_metadata" in splash_source
    assert "on_back=_show_map_library_surface" in splash_source
    assert "_set_active_navigation(\"Map Library\")" in splash_source
    assert "This describes the cave system, not necessarily this 3D map." in details_source
    assert "on_open_source" in details_source
    assert 'text="‹  Map Library"' in details_source
    assert "highlightthickness=0" in details_source
    assert "Focus the neutral detail surface without outlining the back link." in details_source


@pytest.mark.parametrize(
    (
        "snapshot",
        "status",
        "action_text",
        "action",
        "status_action",
        "action_replaces_status_after_delay",
    ),
    [
        (
            UpdateSnapshot(
                state=UpdateState.AVAILABLE,
                current_version="1.0.77",
                available_version="1.0.78",
            ),
            "",
            "Update to 1.0.78",
            splash_screen._UpdateAction.DOWNLOAD,
            None,
            False,
        ),
        (
            UpdateSnapshot(
                state=UpdateState.DOWNLOADING,
                current_version="1.0.63",
                available_version="1.0.64",
                downloaded_bytes=42,
                total_bytes=100,
            ),
            "Downloading… 42%",
            "Cancel",
            splash_screen._UpdateAction.CANCEL,
            None,
            False,
        ),
        (
            UpdateSnapshot(
                state=UpdateState.VERIFYING,
                current_version="1.0.63",
                available_version="1.0.64",
            ),
            "Verifying…",
            "Cancel",
            splash_screen._UpdateAction.CANCEL,
            None,
            False,
        ),
        (
            UpdateSnapshot(
                state=UpdateState.READY,
                current_version="1.0.63",
                available_version="1.0.64",
                payload_path="/downloads/CaveViewer.dmg",
                reveal_action_label="Show in Finder",
            ),
            "Update ready",
            "Show in Finder",
            splash_screen._UpdateAction.REVEAL,
            None,
            True,
        ),
        (
            UpdateSnapshot(
                state=UpdateState.FAILED,
                current_version="1.0.63",
                available_version="1.0.64",
                error="offline",
            ),
            "Download failed",
            "Retry",
            splash_screen._UpdateAction.RETRY,
            None,
            False,
        ),
    ],
)
def test_update_state_has_expected_splash_presentation(
    snapshot,
    status,
    action_text,
    action,
    status_action,
    action_replaces_status_after_delay,
):
    presentation = splash_screen._update_presentation(snapshot)

    assert presentation.status_text == status
    assert presentation.action_text == action_text
    assert presentation.action == action
    assert presentation.status_action == status_action
    assert (
        presentation.action_replaces_status_after_delay
        is action_replaces_status_after_delay
    )


@pytest.mark.parametrize(
    "state",
    (
        UpdateState.IDLE,
        UpdateState.CHECKING,
        UpdateState.UP_TO_DATE,
        UpdateState.SHUTDOWN,
    ),
)
def test_non_actionable_update_states_remain_quiet(state):
    presentation = splash_screen._update_presentation(
        UpdateSnapshot(state=state, current_version="1.0.63"),
    )

    assert presentation == splash_screen._UpdatePresentation()


class _FakeUpdateActionLabel:
    def __init__(self):
        self.bindings = {}
        self.options = {}
        self.unbound = []

    def unbind(self, sequence):
        self.unbound.append(sequence)
        self.bindings.pop(sequence, None)

    def config(self, **options):
        self.options.update(options)

    def bind(self, sequence, callback):
        self.bindings[sequence] = callback


class _FakeUpdateActionManager:
    def __init__(self):
        self.calls = []

    def start_download(self):
        self.calls.append("start")

    def start_installation(self):
        self.calls.append("install")
        return True

    def cancel_download(self):
        self.calls.append("cancel")

    def reveal_download(self):
        self.calls.append("reveal")


@pytest.mark.parametrize("sequence", ("<Button-1>", "<Return>", "<space>"))
def test_cancel_update_action_accepts_pointer_and_keyboard_activation(sequence):
    label = _FakeUpdateActionLabel()
    manager = _FakeUpdateActionManager()

    splash_screen._bind_update_label_action(
        label,
        manager,
        splash_screen._UpdateAction.CANCEL,
    )

    assert label.options == {"takefocus": True}
    assert label.unbound == ["<Button-1>", "<Return>", "<space>"]
    assert set(label.bindings) == {"<Button-1>", "<Return>", "<space>"}
    assert label.bindings[sequence]() == "break"
    assert manager.calls == ["cancel"]


def test_install_update_action_requests_the_explicit_manager_handoff():
    label = _FakeUpdateActionLabel()
    manager = _FakeUpdateActionManager()

    splash_screen._bind_update_label_action(
        label,
        manager,
        splash_screen._UpdateAction.INSTALL,
    )

    assert label.bindings["<Button-1>"]() == "break"
    assert manager.calls == ["install"]


def test_disabled_update_gate_shows_its_safe_explanation_without_an_action():
    presentation = splash_screen._update_presentation(
        UpdateSnapshot(
            state=UpdateState.IDLE,
            current_version="1.0.63",
            automatic_update=FeatureDecision(
                feature=FeatureId.AUTOMATIC_UPDATE,
                state=FeatureState.DISABLED,
                reason_code="automatic_update_target_unsupported",
                explanation="Automatic updates are unavailable for this installation.",
            ),
        ),
    )

    assert presentation == splash_screen._UpdatePresentation(
        status_text="Automatic updates are unavailable for this installation."
    )


def test_disabled_update_package_reveal_gate_hides_ready_action():
    presentation = splash_screen._update_presentation(
        UpdateSnapshot(
            state=UpdateState.READY,
            current_version="1.0.63",
            available_version="1.0.64",
            payload_path="/downloads/CaveViewer.bin",
            update_package_reveal=FeatureDecision(
                feature=FeatureId.UPDATE_PACKAGE_REVEAL,
                state=FeatureState.DISABLED,
                reason_code="update_package_reveal_route_unsupported",
                explanation="The verified update package cannot be revealed automatically.",
            ),
        ),
    )

    assert presentation == splash_screen._UpdatePresentation(
        status_text="The verified update package cannot be revealed automatically."
    )


def test_ready_update_uses_the_snapshot_label_as_the_reveal_action():
    presentation = splash_screen._update_presentation(
        UpdateSnapshot(
            state=UpdateState.READY,
            current_version="1.0.63",
            available_version="1.0.64",
            payload_path="/downloads/CaveViewer.AppImage",
            reveal_action_label="Open Download Folder",
        )
    )

    assert splash_screen._update_status_label(presentation) == (
        "Update ready",
        splash_screen._INSTRUCTION_COLOR,
        None,
    )
    assert splash_screen._update_status_label(
        presentation,
        show_delayed_action=True,
    ) == (
        "Open Download Folder",
        splash_screen._BUTTON_BG,
        splash_screen._UpdateAction.REVEAL,
    )


def test_windows_exe_update_prompts_for_install_restart_and_never_uses_reveal():
    available = splash_screen._update_presentation(
        UpdateSnapshot(
            state=UpdateState.AVAILABLE,
            current_version="1.0.63",
            available_version="1.0.64",
            install_action_label="Install and restart",
        )
    )
    ready = splash_screen._update_presentation(
        UpdateSnapshot(
            state=UpdateState.READY,
            current_version="1.0.63",
            available_version="1.0.64",
            payload_path=r"C:\Users\Ada\AppData\Local\CaveViewer\updates\CaveViewerSetup.exe",
            install_action_label="Install and restart",
        )
    )
    handoff = splash_screen._update_presentation(
        UpdateSnapshot(
            state=UpdateState.HANDOFF_VERIFYING,
            current_version="1.0.63",
        )
    )

    assert available == splash_screen._UpdatePresentation(
        action_text="Install and restart 1.0.64",
        action=splash_screen._UpdateAction.INSTALL,
    )
    assert ready == splash_screen._UpdatePresentation(
        status_text="Update ready",
        action_text="Install and restart",
        action=splash_screen._UpdateAction.INSTALL,
    )
    assert handoff == splash_screen._UpdatePresentation(
        status_text="Verifying installer…"
    )


def test_preview_install_actions_do_not_repeat_the_package_channel():
    available = splash_screen._update_presentation(
        UpdateSnapshot(
            state=UpdateState.AVAILABLE,
            current_version="1.0.85",
            update_channel="preview",
            available_version="1.0.86",
            install_action_label="Install and restart",
        )
    )
    ready = splash_screen._update_presentation(
        UpdateSnapshot(
            state=UpdateState.READY,
            current_version="1.0.85",
            update_channel="preview",
            available_version="1.0.86",
            install_action_label="Install and restart",
        )
    )

    assert available.action_text == "Install and restart 1.0.86"
    assert ready.action_text == "Install and restart"


def test_preview_update_states_are_explicitly_labeled():
    available = splash_screen._update_presentation(
        UpdateSnapshot(
            state=UpdateState.AVAILABLE,
            current_version="1.0.63",
            update_channel="preview",
            available_version="1.0.64",
        )
    )
    downloading = splash_screen._update_presentation(
        UpdateSnapshot(
            state=UpdateState.DOWNLOADING,
            current_version="1.0.63",
            update_channel="preview",
            downloaded_bytes=50,
            total_bytes=100,
        )
    )
    verifying = splash_screen._update_presentation(
        UpdateSnapshot(
            state=UpdateState.VERIFYING,
            current_version="1.0.63",
            update_channel="preview",
        )
    )
    ready = splash_screen._update_presentation(
        UpdateSnapshot(
            state=UpdateState.READY,
            current_version="1.0.63",
            update_channel="preview",
        )
    )

    assert available.action_text == "Update to 1.0.64"
    assert downloading.status_text == "Downloading… 50%"
    assert verifying.status_text == "Verifying…"
    assert ready.status_text == "Update ready"


def test_requested_windows_installation_shows_handoff_progress_and_retry():
    preparing = splash_screen._update_presentation(
        UpdateSnapshot(
            state=UpdateState.READY,
            current_version="1.0.63",
            install_action_label="Install and restart",
            install_requested=True,
        )
    )
    installing = splash_screen._update_presentation(
        UpdateSnapshot(state=UpdateState.INSTALLING, current_version="1.0.63")
    )
    failed = splash_screen._update_presentation(
        UpdateSnapshot(
            state=UpdateState.FAILED,
            current_version="1.0.63",
            install_action_label="Install and restart",
            install_requested=True,
        )
    )

    assert preparing == splash_screen._UpdatePresentation(status_text="Preparing update…")
    assert installing == splash_screen._UpdatePresentation(status_text="Starting update…")
    assert failed == splash_screen._UpdatePresentation(
        status_text="Update download failed",
        action_text="Retry installation",
        action=splash_screen._UpdateAction.RETRY,
        error=True,
    )


def test_last_browse_directory_uses_xdg_state_home(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    state_home = tmp_path / "state"
    map_root = tmp_path / "maps"
    map_root.mkdir()
    monkeypatch.setenv("XDG_STATE_HOME", str(state_home))

    splash_screen._save_last_browse_dir(str(map_root))

    state_file = state_home / "caveviewer" / "last_browse_path"
    assert state_file.read_text(encoding="utf-8") == str(map_root)
    assert splash_screen._load_last_browse_dir() == str(map_root)


def test_library_recent_maps_use_open_history_not_last_browse(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(sys, "platform", "linux")
    state_home = tmp_path / "state"
    last_map = tmp_path / "last-map"
    recent_map = tmp_path / "recent-map"
    last_map.mkdir()
    recent_map.mkdir()
    monkeypatch.setenv("XDG_STATE_HOME", str(state_home))

    splash_screen._save_last_browse_dir(str(last_map))
    map_history.remember_recent_map_path(str(recent_map))

    assert splash_screen._load_library_recent_map_paths() == [str(recent_map)]


@pytest.mark.parametrize("platform_name", ["darwin", "win32"])
def test_splash_root_reuses_existing_retained_tk_root(monkeypatch, platform_name):
    monkeypatch.setattr(
        splash_screen,
        "_SPLASH_LAYOUT_POLICY",
        select_presentation_profile(platform_name=platform_name).splash_layout,
    )
    destroyed_children = []

    class Child:
        def __init__(self, name):
            self.name = name

        def destroy(self):
            destroyed_children.append(self.name)

    class ExistingRoot:
        def winfo_exists(self):
            return True

        def winfo_children(self):
            return [Child("old-logo"), Child("old-button")]

    root = ExistingRoot()
    tk_module = type(
        "FakeTk",
        (),
        {
            "_default_root": root,
            "Tk": lambda **_options: (_ for _ in ()).throw(
                AssertionError("retained-root platform must reuse the Tk root")
            ),
        },
    )

    assert splash_screen._create_splash_root(tk_module) is root
    assert destroyed_children == ["old-logo", "old-button"]


def test_splash_root_creates_new_tk_when_no_macos_root(monkeypatch):
    monkeypatch.setattr(
        splash_screen,
        "_SPLASH_LAYOUT_POLICY",
        select_presentation_profile(platform_name="darwin").splash_layout,
    )
    created = []
    root = object()
    tk_module = type(
        "FakeTk",
        (),
        {
            "_default_root": None,
            "Tk": lambda **options: created.append(options) or root,
        },
    )

    assert splash_screen._create_splash_root(tk_module) is root
    assert created == [splash_screen.tk_root_options()]


def test_splash_root_does_not_reuse_default_root_off_macos(monkeypatch):
    monkeypatch.setattr(
        splash_screen,
        "_SPLASH_LAYOUT_POLICY",
        select_presentation_profile(platform_name="unsupported").splash_layout,
    )
    existing_root = object()
    created = []
    root = object()
    tk_module = type(
        "FakeTk",
        (),
        {
            "_default_root": existing_root,
            "Tk": lambda **options: created.append(options) or root,
        },
    )

    assert splash_screen._create_splash_root(tk_module) is root
    assert created == [splash_screen.tk_root_options()]


def test_splash_font_selection_prefers_tk_visible_linux_family():
    selected = splash_screen._select_tk_font_family(
        {"noto sans": "Noto Sans"},
        "TkDefaultFont",
        ["Missing Family", *splash_screen._LINUX_TK_SANS_FAMILIES],
        linux_layout=True,
    )

    assert selected == "Noto Sans"


def test_splash_font_selection_avoids_nimbus_sans_linux_fallback():
    selected = splash_screen._select_tk_font_family(
        {},
        "Nimbus Sans L",
        ["Missing Family"],
        linux_layout=True,
    )

    assert selected == "sans-serif"


def test_splash_font_configuration_does_not_wait_on_fontconfig():
    source = inspect.getsource(splash_screen)

    assert "subprocess.run" not in source
    assert "_fontconfig_sans_family" not in source


def test_splash_fonts_scale_from_runtime_tk_default(monkeypatch):
    font_globals = (
        "_UI_FONT_FAMILY",
        "_TK_TEXT_SCALE",
        "_TYPOGRAPHY",
    )
    original_values = {name: getattr(splash_screen, name) for name in font_globals}

    class FakeDefaultFont:
        def actual(self, key):
            return {"family": "Helvetica Neue", "size": 15}[key]

    import tkinter.font as tkfont

    monkeypatch.setattr(tkfont, "families", lambda _root: ["Helvetica Neue"])
    monkeypatch.setattr(tkfont, "nametofont", lambda _name: FakeDefaultFont())
    monkeypatch.setattr(splash_screen, "_LINUX_SPLASH_LAYOUT", False)

    try:
        splash_screen._configure_runtime_tk_fonts(
            object(),
            presentation_profile=select_presentation_profile(platform_name="darwin"),
        )

        assert splash_screen._TK_TEXT_SCALE == pytest.approx(1.4)
        assert splash_screen._TYPOGRAPHY.display == (
            "Helvetica Neue",
            28,
            "bold",
        )
        assert splash_screen._TYPOGRAPHY.heading == ("Helvetica Neue", 22, "bold")
        assert splash_screen._TYPOGRAPHY.body_strong == (
            "Helvetica Neue",
            17,
            "bold",
        )
        assert splash_screen._TYPOGRAPHY.body == ("Helvetica Neue", 17)
        assert splash_screen._TYPOGRAPHY.supporting == ("Helvetica Neue", 14)
        assert splash_screen._TYPOGRAPHY.section == (
            "Helvetica Neue",
            14,
            "bold",
        )
    finally:
        for name, value in original_values.items():
            setattr(splash_screen, name, value)


def test_splash_linux_fonts_do_not_multiply_the_tk_default_font(monkeypatch):
    font_globals = (
        "_UI_FONT_FAMILY",
        "_TK_TEXT_SCALE",
        "_TYPOGRAPHY",
    )
    original_values = {name: getattr(splash_screen, name) for name in font_globals}

    class FakeDefaultFont:
        def actual(self, key):
            return {"family": "sans-serif", "size": 18}[key]

    import tkinter.font as tkfont

    monkeypatch.setattr(tkfont, "families", lambda _root: ["sans-serif"])
    monkeypatch.setattr(tkfont, "nametofont", lambda _name: FakeDefaultFont())

    try:
        splash_screen._configure_runtime_tk_fonts(
            object(),
            presentation_profile=select_presentation_profile(platform_name="linux"),
        )

        assert splash_screen._TK_TEXT_SCALE == pytest.approx(1.0)
        assert splash_screen._TYPOGRAPHY.body_strong == (
            "sans-serif",
            12,
            "bold",
        )
        assert splash_screen._TYPOGRAPHY.supporting == ("sans-serif", 10)
    finally:
        for name, value in original_values.items():
            setattr(splash_screen, name, value)


def test_launch_layout_settlement_is_fixed_and_bounded():
    calls = []
    root = SimpleNamespace(update_idletasks=lambda: calls.append(True))

    splash_screen._settle_launch_layout(root, passes=3)

    assert calls == [True, True, True]


def test_preferences_navigation_gear_geometry_executes_during_startup():
    points = splash_screen._navigation_gear_points(16.0, lambda value: value)

    assert len(points) == 16
    assert points[0] == pytest.approx((16.0, 5.0))
    assert points[8] == pytest.approx((16.0, 27.0))


def test_launch_splash_waits_only_for_the_remaining_minimum_duration():
    from caveviewer.gui.splash_controller import StartupReadinessGate

    gate = StartupReadinessGate(
        visible_at=10.0,
        minimum_ms=splash_screen._MIN_LAUNCH_SPLASH_MS,
    )
    assert splash_screen._MIN_LAUNCH_SPLASH_MS == 3_000
    assert gate.remaining_delay_ms(10.0) == 3_000
    assert gate.remaining_delay_ms(11.25) == 1_750
    assert gate.remaining_delay_ms(13.0) == 0
    assert gate.remaining_delay_ms(16.0) == 0


def test_launch_splash_uses_the_loading_exploration_tagline():
    source = inspect.getsource(splash_screen._render_launch_content)

    assert 'text="Preparing to explore what lies beneath..."' in source
    assert "program_name" not in source
    assert 'text="Starting…"' not in source


def test_launch_surface_uses_a_flat_milestone_progress_bar():
    source = inspect.getsource(splash_screen._render_launch_content)
    calls = []

    class _Canvas:
        def winfo_width(self):
            return 132

        def winfo_height(self):
            return 132

        def delete(self, tag):
            calls.append(("delete", tag))

        def create_text(self, *coordinates, **options):
            calls.append(("text", coordinates, options))

        def create_rectangle(self, *coordinates, **options):
            calls.append(("rectangle", coordinates, options))

    splash_screen._render_launch_content(
        _Canvas(),
        progress=0.5,
        px=lambda value: int(value),
    )

    assert "create_rectangle(" in source
    assert "create_oval(" not in source
    assert "create_arc(" not in source
    assert calls[0] == ("delete", "launch_content")
    assert calls[1][0] == "text"
    assert calls[1][2]["text"] == "Preparing to explore what lies beneath..."
    assert [call[0] for call in calls].count("rectangle") == 2


def test_launch_logo_suppresses_only_amber_pixels_like_the_map_loader():
    from PIL import Image

    logo = Image.new("RGBA", (3, 1))
    logo.putdata(
        (
            (229, 161, 31, 255),
            (120, 80, 50, 20),
            (45, 165, 210, 255),
        )
    )

    filtered = splash_screen._suppress_amber_logo_pixels(logo)

    assert list(filtered.get_flattened_data()) == [
        (229, 161, 31, 0),
        (120, 80, 50, 0),
        (45, 165, 210, 255),
    ]


def test_launch_surface_uses_dark_background_without_a_logo_or_ring():
    source = inspect.getsource(splash_screen._build_launch_surface)
    content_source = inspect.getsource(splash_screen._render_launch_content)

    assert "_load_brand_logo(" not in source
    assert "progress_ring_photo(" not in content_source
    assert "program_name" not in content_source
    assert "_render_launch_background(" in source


def test_splash_navigation_actions_are_keyboard_accessible_without_fallthrough():
    source = inspect.getsource(splash_screen.show_splash_screen)
    update_action_source = inspect.getsource(splash_screen._bind_update_label_action)
    background_layout_source = source[
        source.index("_create_map_library_panel(map_library_surface)") : source.index(
            "content_frame.tkraise()"
        )
    ]

    assert "navigation_frame = tk.Frame(" in source
    assert "def _create_navigation_icon(" in source
    assert "def _create_navigation_item(" in source
    assert "item_row = tk.Frame(navigation_frame, bg=_BG_COLOR)" in source
    assert "icon = _create_navigation_icon(item_row, icon_name)" in source
    assert "_bind_activation(icon, callback)" in source
    assert "font=_TYPOGRAPHY.body_strong if selected else _TYPOGRAPHY.body" in source
    assert "takefocus=True" in source
    assert 'label.bind("<Return>", invoke)' in update_action_source
    assert 'label.bind("<space>", invoke)' in update_action_source
    assert "def _invoke_and_break(callback):" in source
    assert "def _bind_activation(widget, callback) -> None:" in source
    assert "map_library_navigation_item = _create_navigation_item(" in source
    assert '"Map Library",' in source
    assert 'icon_name="map"' in source
    assert '_create_navigation_item("Open Map", on_open_map_folder)' not in source
    assert "preferences_navigation_item = _create_navigation_item(" in source
    assert 'icon_name="preferences"' in source
    assert 'icon_name="help"' in source
    assert 'icon_name="about"' in source
    assert "about_navigation_item = _create_navigation_item(" in source
    assert "open_map_folder=on_open_map_folder" in source
    assert "def _focus_map_library() -> None:" in source
    assert "panel.focus_content()" in source
    assert "map_library_surface = tk.Frame(right_frame, bg=_BG_COLOR)" in source
    assert "preferences_surface = tk.Frame(right_frame, bg=_BG_COLOR)" in source
    assert "help_surface = tk.Frame(right_frame, bg=_BG_COLOR)" in source
    assert "about_surface = tk.Frame(right_frame, bg=_BG_COLOR)" in source
    assert "def _show_preferences_surface() -> None:" in source
    assert "preferences_surface_required_height" not in source
    assert "panel = _ensure_preferences_panel()" in source
    assert "_ensure_preferences_panel()" in background_layout_source
    assert "map_library_surface.pack_forget()" not in source
    assert "preferences_surface.pack_forget()" not in source
    assert 'surface.grid(row=0, column=0, sticky="nsew")' in source
    assert "preferences_surface.tkraise()" in source
    assert "root.after_idle(_ensure_preferences_panel)" not in source
    assert "_build_launch_surface(" in source
    assert "_settle_launch_layout(root, passes=3)" in source
    assert "readiness_gate = StartupReadinessGate(" in source
    assert "def _advance_launch_progress(fraction: float) -> None:" in source
    assert "readiness_gate.advance(fraction)" in source
    assert "readiness_gate.mark_ready()" in source
    assert "def _reveal_composed_main_surface() -> None:" in source
    assert "splash_controller.schedule(" in source
    return_session_reveal = source[
        source.index("if show_launch_overlay:", source.index("def _animate_launch_progress")) :
        source.index("# The app-owned manager survives", source.index("def _animate_launch_progress"))
    ]
    assert "else:\n        # Returning from a native viewer" in return_session_reveal
    assert "_reveal_composed_main_surface()" in return_session_reveal
    assert "schedule_idle" not in return_session_reveal
    assert source.index("_reveal_composed_main_surface()", source.index("else:")) < source.index(
        "root.mainloop()"
    )
    reveal_source = source[
        source.index("def _reveal_composed_main_surface() -> None:") : source.index(
            "def _animate_launch_progress() -> None:"
        )
    ]
    assert "if not readiness_gate.ready:" in reveal_source
    assert "readiness_gate.remaining_delay_ms(time.monotonic())" in reveal_source
    assert "launch_surface.destroy()" in reveal_source
    assert "root.resizable(" not in reveal_source
    assert "after_idle(" not in reveal_source
    assert "schedule_after(" not in reveal_source
    assert source.count("root.resizable(True, True)") == 1
    assert source.index("root.resizable(True, True)") < source.index("root.deiconify()")
    assert "root.resizable(False, False)" not in source
    assert "available_width = max(1, screen_w - display_margin)" in source
    assert "available_height = max(1, screen_h - display_margin)" in source
    assert "root.minsize(" in source
    assert "min(px(_SPLASH_RESIZE_MIN_WIDTH), available_width)" in source
    assert "min(px(_SPLASH_RESIZE_MIN_HEIGHT), available_height)" in source
    assert "final_height = min(final_height, available_height)" in source
    assert "if show_launch_overlay" in source
    assert "readiness_gate.visual_progress(now)" in source
    assert "_LAUNCH_PROGRESS_INTERVAL_MS" in source
    assert "splash_controller.schedule(50, _reveal_composed_main_surface)" in source
    assert "def _show_about_surface() -> None:" in source
    assert "def _ensure_help_panel() -> HelpPanel:" in source
    assert "def _show_help_surface() -> None:" in source
    assert "_request_leave_preferences(_show_help_surface)" in source
    assert "PreferencesPanel(" in source
    assert "HelpPanel(" in source
    assert "keyboard_control_sections(presentation_profile)" in source
    assert "TroubleshootingLogController(" in source
    assert "application_log_directory(" in source
    assert "platform_runtime.diagnostic_log_reveal_adapter" in source
    assert "_build_themed_about_content(" in source
    assert "show_close=False" in source
    assert "def _request_leave_preferences" in source
    assert "def _prepare_surface_change" in source
    assert 'panel.on_hidden()' in source
    assert "_show_unsaved_preferences_dialog(" in source
    assert "on_save=panel.apply" in source
    assert "on_continue=next_action" in source
    assert source.index("help_navigation_item = _create_navigation_item") < source.index(
        "about_navigation_item = _create_navigation_item"
    )
    assert 'navigation_items["Help"] = help_navigation_item' in source
    assert 'active_surface[0] in {"about", "help"}' in source
    assert 'root.bind("<Return>", _handle_root_return)' in source
    assert 'root.bind("<Escape>", _cancel_preferences_or_close)' in source
    assert "_show_themed_about_dialog(" not in source
    assert "browse_button_frame" not in source
    assert 'text="Open map…"' not in source
    assert "open_recorded_dive_link" not in source
    assert 'text="Open recorded dive…"' not in source
    assert "instruction_label" not in source
    assert '"Maps use .glb, or .obj with matching .mtl and textures."' not in source
    assert "def on_open(event=None):" not in source
    assert "preferences_link" not in source
    assert "MapLibraryWorkflow(" in source
    assert "load_initial_standard_library_catalog" in source
    assert "KNOWN_STANDARD_LIBRARY_MAPS" not in source
    assert "start_sample_download_worker(" not in source
    assert "show_sample_maps_dialog(" not in source


def test_splash_navigation_keeps_asymmetric_label_padding_in_pack_geometry():
    source = textwrap.dedent(inspect.getsource(splash_screen.show_splash_screen))
    tree = ast.parse(source)
    label_calls = [
        call
        for call in ast.walk(tree)
        if isinstance(call, ast.Call)
        and isinstance(call.func, ast.Attribute)
        and isinstance(call.func.value, ast.Name)
        and call.func.value.id == "tk"
        and call.func.attr == "Label"
    ]

    assert label_calls
    assert all(
        not (
            keyword.arg == "padx" and isinstance(keyword.value, ast.Tuple)
        )
        for call in label_calls
        for keyword in call.keywords
    )
    assert 'item.pack(side="left", fill="both", expand=True, padx=(0, px(11)))' in source


def test_splash_navigation_uses_consistent_row_spacing():
    source = inspect.getsource(splash_screen.show_splash_screen)

    assert splash_screen._NAVIGATION_ITEM_GAP == 8
    assert 'item_row.pack(fill="x", pady=(0, px(_NAVIGATION_ITEM_GAP)))' in source


def test_splash_navigation_selection_uses_type_and_color_without_a_fill():
    source = inspect.getsource(splash_screen.show_splash_screen)

    assert "_NAVIGATION_ACTIVE_BG" not in inspect.getsource(splash_screen)
    assert "background = _NAVIGATION_HOVER_BG if active else _BG_COLOR" in source
    assert "_TYPOGRAPHY.body_strong" in source


def test_unsaved_preferences_dialog_offers_three_explicit_close_choices():
    source = inspect.getsource(splash_screen._show_unsaved_preferences_dialog)

    assert '_make_button("Save"' in source
    assert '_make_button("Discard"' in source
    assert '_make_button("Keep"' in source
    assert 'button_row.pack(side="bottom", fill="x")' in source
    assert "MODAL_MIN_WIDTH" in source
    assert "MODAL_MIN_HEIGHT" in source
    assert "if not on_save():" in source
    assert 'dialog.bind("<Escape>", _close_dialog)' in source
    assert 'dialog.protocol("WM_DELETE_WINDOW", _close_dialog)' in source
    assert "save_button.focus_set()" in source


def test_preferences_and_help_share_a_compact_embedded_panel_type_scale():
    source = inspect.getsource(splash_screen.show_splash_screen)
    help_style_source = inspect.getsource(splash_screen._help_panel_style)

    assert splash_screen._EMBEDDED_PANEL_TEXT_SCALE_FACTOR == pytest.approx(11 / 12)
    assert "typography=_embedded_panel_typography()" in source
    assert "typography = _embedded_panel_typography()" in help_style_source


def test_splash_navigation_uses_a_quiet_rail_and_lower_app_status():
    source = inspect.getsource(splash_screen.show_splash_screen)
    update_source = inspect.getsource(splash_screen._update_presentation)

    assert "_TYPOGRAPHY: TkTypography = create_tk_typography(" in inspect.getsource(
        splash_screen
    )
    assert "brand_frame = tk.Frame(left_frame, bg=_BG_COLOR)" not in source
    assert "masthead_icon_label" not in source
    assert 'navigation_frame.pack(fill="x", pady=(px(22), 0))' in source
    assert "app_status_frame = tk.Frame(left_frame, bg=_BG_COLOR)" in source
    assert 'text=f"Version {version}"' in source
    assert 'version_label.pack(anchor="w")' in source
    assert "update_cluster = tk.Frame(app_status_frame, bg=_BG_COLOR)" in source
    assert "def _set_update_cluster_visible(visible: bool)" in source
    assert "def _layout_update_cluster(presentation: _UpdatePresentation)" in source
    assert "Preview update" not in update_source
    assert "Preview installer" not in update_source
    assert 'action_text = f"{action_text} {snapshot.available_version}"' in update_source
    assert "action_text=snapshot.reveal_action_label" in update_source
    assert "action_replaces_status_after_delay=True" in update_source
    assert "_UPDATE_READY_ACTION_DELAY_MS = 3_000" in inspect.getsource(
        splash_screen
    )
    assert "def _show_delayed_update_action(" in source
    assert "show_delayed_action=True" in source
    assert "update_cluster.pack_forget()" in source
    assert "update_cancel_button.pack_forget()" in source
    assert 'update_label.pack(side="left", anchor="w", fill="x", expand=True)' in source
    assert 'update_cancel_button.pack(side="right", padx=(px(6), 0))' in source
    assert "def _draw_update_cancel_button(progress_fraction: float)" in source
    assert "extent_degrees=-360 * clamped" in source
    update_cancel_source = source[
        source.index("def _draw_update_cancel_button(") : source.index(
            "def _set_update_cluster_visible("
        )
    ]
    assert "progress_control_photo(" in update_cancel_source
    assert "create_rectangle(" not in update_cancel_source
    assert "update_progress_canvas" not in source
    footer_action_source = source[
        source.index("update_action_label = tk.Label(") : source.index(
            "update_cancel_button = tk.Canvas("
        )
    ]
    assert "font=_TYPOGRAPHY.supporting" in footer_action_source
    assert 'justify="left"' in source


def test_themed_about_content_owns_the_brand_identity_while_launch_stays_quiet():
    content_source = inspect.getsource(splash_screen._build_themed_about_content)
    logo_source = inspect.getsource(splash_screen._load_brand_logo)
    launch_source = inspect.getsource(splash_screen._build_launch_surface)
    dialog_source = inspect.getsource(splash_screen._show_themed_about_dialog)
    splash_source = inspect.getsource(splash_screen.show_splash_screen)

    assert "tk.Toplevel(root)" in dialog_source
    assert "_build_themed_about_content(" in dialog_source
    assert "dialog.grab_set()" in dialog_source
    assert "_LOGO_PATH" in logo_source
    assert "_load_brand_logo(" in content_source
    assert "_load_brand_logo(" not in launch_source
    assert "_CREDITS_TEXT.strip()" in content_source
    assert "_ABOUT_WEBSITE_LINKS" in content_source
    assert "www.caveviewer.com" in inspect.getsource(splash_screen)
    assert "www.bottomlineprojects.com" in inspect.getsource(splash_screen)
    assert "on_open_website: Callable[[str], None] | None = None" in content_source
    assert 'website_label.pack(pady=(px(12) if index == 0 else px(6), 0))' in content_source
    credits_source = content_source[
        content_source.index("text=_CREDITS_TEXT.strip()") : content_source.index(
            "for index, (label_text, website_url)"
        )
    ]
    websites_source = content_source[
        content_source.index("for index, (label_text, website_url)") : content_source.index(
            "close_button = content"
        )
    ]
    assert "font=_TYPOGRAPHY.body" in credits_source
    assert "wraplength=px(_ABOUT_CREDITS_WRAP_LENGTH)" in credits_source
    assert "font=_TYPOGRAPHY.body" in websites_source
    assert splash_screen._ABOUT_CREDITS_WRAP_LENGTH == 430
    assert "_open_about_website" in splash_source
    assert "on_open_website=_open_about_website" in splash_source
    assert "bg=_BG_COLOR" in content_source
    assert "fg=_TITLE_COLOR" in content_source
    assert "text=\"Close\"" in content_source
    assert "show_close: bool = True" in content_source
    assert "center_vertically" in content_source
    assert "credits_panel" not in content_source
    assert "highlightbackground=_BORDER_COLOR" not in content_source


def test_cache_rebuild_starts_from_splash_without_a_confirmation_window():
    splash_source = inspect.getsource(splash_screen.show_splash_screen)

    assert "CacheRebuildJobController(" in splash_source
    assert "runtime_settings_provider=" in splash_source
    assert "confirm_cache_rebuild=" not in splash_source
    assert "_confirm_cache_rebuild_dialog" not in splash_source
    assert "request_cache_rebuild_pause" in splash_source
    assert "def _splash_is_foreground()" in splash_source
    assert "splash_is_foreground=_splash_is_foreground" in splash_source


def test_splash_map_picker_checks_its_directory_selection_route_before_calling_it():
    source = inspect.getsource(splash_screen.show_splash_screen)

    assert "directory_selection_preflight(" in source
    assert "choose_authorized_directory(" in source
    assert source.index("directory_selection_preflight(") < source.index(
        "choose_authorized_directory("
    )


def test_splash_map_library_uses_navigation_and_an_overflow_cue():
    splash_source = inspect.getsource(splash_screen.show_splash_screen)
    style_source = inspect.getsource(splash_screen._map_library_panel_style)
    controller_source = inspect.getsource(map_library_controller.MapLibraryController)
    panel_source = inspect.getsource(map_library_panel.MapLibraryPanel)
    workflow_source = inspect.getsource(map_library_workflow.MapLibraryWorkflow)
    transfer_source = inspect.getsource(map_library_transfers)
    source = (
        splash_source
        + style_source
        + controller_source
        + panel_source
        + workflow_source
    )
    section_source = panel_source[
        panel_source.find("def _create_section") : panel_source.find(
            "def _create_empty_note"
        )
    ]

    assert '"Map Library"' in splash_source
    assert "Your Recent Maps" in source
    assert "CaveViewer Maps" in source
    assert "Your Library" not in source
    assert "Standard Library" not in source
    assert "Open your maps or explore the standard library." not in source
    assert "No maps added yet." in source
    assert "Maps you open yourself will appear here." not in source
    assert "No user-opened maps yet." not in source
    assert 'top_pad=16' in source
    assert 'bottom_pad=18' in source
    assert "Open a local map" in panel_source
    assert "Browse a cave map folder" in panel_source
    assert "def _create_open_map_action" in panel_source
    assert "def _draw_open_map_action" in panel_source
    assert "self._open_map_folder = open_map_folder" in panel_source
    panel_create_source = panel_source[
        panel_source.index("def create(self, parent)") : panel_source.index(
            "def sync_scroll_region"
        )
    ]
    assert "highlightthickness=0" in panel_create_source
    assert "highlightbackground=style.panel_border_color" not in panel_create_source
    assert "open_map_shell = tk.Frame(panel, bg=style.panel_color)" in panel_source
    assert "self._create_open_map_action(open_map_shell)" in panel_source
    assert "scroll_row = 1" in panel_source
    assert '"Recent Maps"' not in source
    assert "Available Maps" not in source
    assert "Open recent or available maps." not in source
    assert "Available to download" not in source
    assert "font=self._style.section_font" in panel_source
    assert "font=self._style.small_font" not in section_source
    assert "tk.Scrollbar(" not in panel_source
    assert "Scroll to browse more maps ↓" not in panel_source
    assert "CanvasVerticalScrollbar(" in panel_source
    assert "self._content_scrollbar" in panel_source
    assert "self._content_overflows" not in panel_source
    assert "self.sync_scroll_region()" in panel_source
    assert "self._content_scrollbar.sync_overflow(content_height)" in panel_source
    assert "self.bind_mousewheel_if_ready(self._rows_frame)" in panel_source
    assert "self.sync_after_row_change()" in panel_source
    assert "recent_map_paths = _load_library_recent_map_paths()" in splash_source
    assert "self.controller.row(" in workflow_source
    assert "detail=row.detail" in panel_source
    assert "wraplength=self._px(250)" not in panel_source
    assert "self._sync_row_title_wraplength(" in panel_source
    assert "highlightthickness=0" in source
    assert "panel_border_color=_LIBRARY_PANEL_BORDER_COLOR" in style_source
    assert 'left_frame = tk.Frame(content_frame, bg=_BG_COLOR, width=px(220))' in source
    assert 'padx=(px(32), 0)' in source
    assert 'panel.pack(fill="both", expand=True, pady=self._px(14))' in panel_source
    assert "title_font=_TYPOGRAPHY.body_strong" in style_source
    assert "body_font=_TYPOGRAPHY.body" in style_source
    assert "supporting_font=_TYPOGRAPHY.supporting" in style_source
    assert "section_font=_TYPOGRAPHY.section" in style_source
    assert "metadata_color=_LIBRARY_METADATA_COLOR" in style_source
    assert "_action_button_pixel_size" in panel_source
    assert "style.action_button_size" in panel_source
    assert "style.action_icon_stroke_width" in panel_source
    assert "style.overflow_button_size" in panel_source
    assert "progress_bar_canvas = tk.Canvas(" not in source
    assert "reserve_progress=True" not in panel_source
    assert "_create_action_button" in panel_source
    assert "_draw_action_stop_progress" in panel_source
    assert "progress_control_photo(" in panel_source
    action_progress_source = inspect.getsource(
        map_library_panel.MapLibraryPanel._draw_action_progress
    )
    assert "button.create_rectangle(" not in action_progress_source
    assert "_draw_download" in panel_source
    download_source = inspect.getsource(map_library_panel.MapLibraryPanel._draw_download)
    assert "create_rectangle" not in download_source
    assert "_draw_retry" in panel_source
    assert "_set_row_open_activation" not in panel_source
    assert "_cv_row_action_widgets" not in panel_source
    assert "stop_fill_color = style.button_fg" in panel_source
    assert "action_progress_ring_diameter=" in style_source
    assert "action_retry_icon_diameter=" in style_source
    assert "action_stop_size=" in style_source
    assert "show_stop_progress=True" in workflow_source
    assert '"Cancel"' not in workflow_source
    assert '"Stopping…"' in workflow_source
    assert "def poll(" in transfer_source
    assert "def close(" in transfer_source
    assert "poll_download_queue" not in workflow_source
    assert "cancel_active_download_for_close" not in workflow_source
    assert "directory_selection_factory" in workflow_source
    assert "start_catalog_fetch" in workflow_source
    assert "poll_download_queue" not in splash_source
    assert 'self.set_standard_row_metadata(key, "Downloading…")' in panel_source
    assert "Downloading… %" not in panel_source
    assert "Local-only former library maps" not in source
    assert "No longer a part of the standard library" in source
    assert "self._draw_vector_photo(" in section_source
    assert "create_polygon(" not in section_source
    assert "progress_control_photo(" in panel_source
    assert 'text="Hide"' not in section_source
    assert 'text="Show"' not in section_source

    style = splash_screen._map_library_panel_style()
    assert style.progress_track_color == splash_screen.DARK_THEME.entry_background
    assert style.progress_fill_color == splash_screen.DARK_THEME.primary_button
    assert style.progress_track_color != style.button_bg
    assert style.progress_fill_color != style.button_bg
    assert style.progress_fill_color == style.button_fg
    assert style.former_map_title_color == splash_screen.DARK_THEME.secondary_text
    assert style.former_map_title_color != style.title_color


def test_map_library_sections_start_expanded_and_toggle_in_place():
    class _FakeSectionContent:
        def __init__(self) -> None:
            self.pack_calls = []
            self.pack_forget_calls = 0

        def pack(self, **options) -> None:
            self.pack_calls.append(options)

        def pack_forget(self) -> None:
            self.pack_forget_calls += 1

    header = object()
    content = _FakeSectionContent()
    section = map_library_panel.MapLibrarySectionWidgets(
        header=header,
        content=content,
        title="CaveViewer Maps",
    )
    panel = object.__new__(map_library_panel.MapLibraryPanel)
    drawn_states = []
    closed_menus = []
    sync_calls = []
    panel._widget_exists = lambda _widget: True
    panel._draw_section_header = lambda target: drawn_states.append(target.expanded)
    panel.close_active_menu = lambda: closed_menus.append(True)
    panel.sync_after_row_change = lambda: sync_calls.append(True)

    assert section.expanded is True

    panel._toggle_section(section)

    assert section.expanded is False
    assert content.pack_forget_calls == 1
    assert content.pack_calls == []

    panel._toggle_section(section)

    assert section.expanded is True
    assert content.pack_calls == [{"fill": "x", "after": header}]
    assert drawn_states == [False, True]
    assert len(closed_menus) == 2
    assert len(sync_calls) == 2


def test_map_library_section_headers_use_adjacent_disclosure_triangles(monkeypatch):
    class _FakeHeader:
        def __init__(self) -> None:
            self.text_calls = []
            self.image_calls = []

        def delete(self, _tag) -> None:
            pass

        def winfo_width(self) -> int:
            return 320

        def winfo_height(self) -> int:
            return 24

        def cget(self, option) -> int:
            return 320 if option == "width" else 24

        def create_text(self, *coordinates, **options):
            self.text_calls.append((coordinates, options))
            return f"text-{len(self.text_calls)}"

        def bbox(self, _item):
            return (2, 0, 118, 24)

        def create_image(self, *coordinates, **options) -> None:
            self.image_calls.append((coordinates, options))

    panel = object.__new__(map_library_panel.MapLibraryPanel)
    panel._widget_exists = lambda _widget: True
    panel._px = lambda value: int(value)
    panel._style = type(
        "Style",
        (),
        {
            "section_font": ("TkDefaultFont", 10, "bold"),
            "instruction_color": "#ffffff",
        },
    )()
    header = _FakeHeader()
    section = map_library_panel.MapLibrarySectionWidgets(
        header=header,
        content=object(),
        title="CaveViewer Maps",
    )
    vector_calls = []

    def capture_vector_icon(_widget, **options):
        vector_calls.append(options)
        return object()

    monkeypatch.setattr(map_library_panel, "vector_icon_photo", capture_vector_icon)

    panel._draw_section_header(section)
    section.expanded = False
    panel._draw_section_header(section)

    assert [options["text"] for _coordinates, options in header.text_calls] == [
        "CaveViewer Maps",
        "CaveViewer Maps",
    ]
    assert len(header.image_calls) == 2
    assert len(vector_calls) == 2
    expanded = vector_calls[0]["polygons"][0]
    collapsed = vector_calls[1]["polygons"][0]
    assert expanded.points[0][1] == expanded.points[1][1] < expanded.points[2][1]
    assert collapsed.points[0][0] == collapsed.points[1][0] < collapsed.points[2][0]
    assert expanded.fill_color == collapsed.fill_color == "#ffffff"


def test_splash_and_library_curved_canvas_art_uses_antialiased_vector_photos():
    navigation_source = inspect.getsource(splash_screen.show_splash_screen)
    panel_source = inspect.getsource(map_library_panel.MapLibraryPanel)

    assert "vector_icon_photo(" in navigation_source
    assert "vector_icon_photo(" in panel_source
    for primitive in ("create_line(", "create_arc(", "create_oval(", "create_polygon("):
        assert primitive not in navigation_source
        assert primitive not in panel_source


def test_former_standard_row_title_uses_a_muted_style_without_moving_the_row():
    class _FakeLabel:
        def __init__(self) -> None:
            self.config_calls = []

        def config(self, **options) -> None:
            self.config_calls.append(options)

    title_label = _FakeLabel()
    panel = object.__new__(map_library_panel.MapLibraryPanel)
    panel.standard_rows = {
        "former-map": SimpleNamespace(title_label=title_label),
    }
    panel._standard_row_former = {}
    panel._widget_exists = lambda _widget: True
    panel._style = SimpleNamespace(
        title_color="#ffffff",
        former_map_title_color="#9a9aa6",
    )

    panel.set_standard_row_former("former-map", True)
    panel.set_standard_row_former("former-map", False)

    assert panel._standard_row_former == {"former-map": False}
    assert title_label.config_calls == [
        {"fg": "#9a9aa6"},
        {"fg": "#ffffff"},
    ]


def test_map_title_wrap_tracks_the_live_text_column_width():
    class _FakeLabel:
        def __init__(self) -> None:
            self.config_calls = []

        def configure(self, **options) -> None:
            self.config_calls.append(options)

    title_label = _FakeLabel()
    panel = object.__new__(map_library_panel.MapLibraryPanel)
    refreshes = []
    panel._widget_exists = lambda widget: widget is title_label
    panel.sync_after_row_change = lambda: refreshes.append(True)

    panel._sync_row_title_wraplength(title_label, 620)
    panel._sync_row_title_wraplength(title_label, 620)
    panel._sync_row_title_wraplength(title_label, 168)

    assert title_label.config_calls == [
        {"wraplength": 620},
        {"wraplength": 168},
    ]
    assert refreshes == [True, True]


def test_map_library_size_updates_are_independent_from_row_metadata():
    class _FakeSizeLabel:
        def __init__(self) -> None:
            self.config_calls = []

        def config(self, **options) -> None:
            self.config_calls.append(options)

    size_label = _FakeSizeLabel()
    panel = object.__new__(map_library_panel.MapLibraryPanel)
    panel.standard_rows = {
        "sample-map": SimpleNamespace(size_label=size_label),
    }
    panel._widget_exists = lambda widget: widget is size_label

    assert panel.set_standard_row_size("sample-map", "70 MB")
    assert size_label.config_calls == [{"text": "70 MB"}]


def test_map_library_rows_use_subtle_overflow_menu_for_management():
    splash_source = inspect.getsource(splash_screen.show_splash_screen)
    style_source = inspect.getsource(splash_screen._map_library_panel_style)
    panel_source = inspect.getsource(map_library_panel.MapLibraryPanel)
    row_menu_source = inspect.getsource(
        map_library_panel.MapLibraryPanel._show_row_menu
    )
    workflow_source = inspect.getsource(map_library_workflow.MapLibraryWorkflow)
    source = splash_source + style_source + panel_source + workflow_source

    assert "Remove from this list" in source
    assert "Remove cache" in source
    assert "Remove map files" in source
    assert "Remove from Recent" not in source
    assert "_create_overflow_button" in panel_source
    assert "_create_recent_overflow_button" not in source
    assert "menu_actions_factory=" in source
    assert "leading_widget_factory=" not in source
    assert "overflow_button=overflow_button" in source
    assert "self.remove_recent_path(path)" in workflow_source
    assert "self.has_cache(path)" in workflow_source
    assert "self.remove_cache(path)" in workflow_source
    assert "self.remove_downloaded(" in workflow_source
    assert "remove_standard_download" in workflow_source
    assert "remove_standard_download" not in splash_source
    assert "show_row_status" in source
    assert "size_text=row.size_text" in panel_source
    assert "def set_standard_row_size" in panel_source
    assert "row_action_widgets.append(size_label)" not in panel_source
    assert "Cache removed" in source
    assert "Couldn’t remove cache" in source
    assert "Couldn’t remove files" in source
    assert "_cv_base_text" in source
    assert "_cv_status_after_id" in source
    assert "Removed downloaded maps for" not in source
    assert "Removed cache for" not in source
    assert "has_managed_map_cache(sample_path)" not in source
    assert "self._recent_container = self._recent_section.content" in panel_source
    assert "self._standard_container = self._standard_section.content" in panel_source
    assert "self.recent_rows" in panel_source
    assert "self._recent_empty_note = self._create_empty_note" in panel_source
    assert "Open dive plan…" in source
    assert "guided_dive_preflight" in workflow_source
    assert "file_selection_preflight" in workflow_source
    assert "choose_authorized_file" in workflow_source
    assert "desktop_services.choose_file(" not in workflow_source
    assert "platform_runtime=platform_runtime" in splash_source
    assert "Open" in source
    assert "button.create_oval(" not in panel_source
    assert "vector_icon_photo(" in panel_source
    assert 'button.pack(side="right", padx=(0, self._px(12))' in panel_source
    assert "padx=(0, self._px(8))" in panel_source
    assert "_install_menu_dismissal_bindings" in panel_source
    assert "tk.Frame(" in row_menu_source
    assert "menu.place(" in row_menu_source
    assert "tk.Toplevel" not in row_menu_source
    assert "menu.geometry" not in row_menu_source
    assert '"<ButtonPress-1>"' in panel_source
    assert '"<FocusOut>"' in panel_source
    assert ".bind_all(" not in panel_source


def test_map_library_menu_outside_click_binding_is_scoped_and_removed_on_close():
    class _FakeRoot:
        def __init__(self) -> None:
            self.callbacks = {}
            self.unbound = []

        def bind(self, sequence, callback, add=None):
            assert add == "+"
            callback_id = f"callback-{len(self.callbacks) + 1}"
            self.callbacks[sequence] = (callback_id, callback)
            return callback_id

        def unbind(self, sequence, callback_id):
            self.unbound.append((sequence, callback_id))

    panel = object.__new__(map_library_panel.MapLibraryPanel)
    root = _FakeRoot()
    menu = object()
    opener = object()
    panel.root = root
    panel._active_menu = menu
    panel._active_menu_root_bindings = []
    panel.recent_rows = {}
    panel.standard_rows = {}
    panel._widget_exists = lambda _widget: False

    panel._install_menu_dismissal_bindings(menu, opener)
    _callback_id, outside_click = root.callbacks["<ButtonPress-1>"]

    outside_click(SimpleNamespace(widget=object()))

    assert panel._active_menu is None
    assert root.unbound == [
        ("<ButtonPress-1>", "callback-1"),
        ("<FocusOut>", "callback-2"),
        ("<Escape>", "callback-3"),
    ]


def test_map_library_menu_popover_position_stays_inside_the_splash():
    position = map_library_panel.MapLibraryPanel._menu_popover_position(
        button_x=760,
        button_y=450,
        button_width=28,
        button_height=28,
        root_width=800,
        root_height=600,
        menu_width=240,
        menu_height=96,
        margin=8,
        gap=4,
    )

    assert position == (548, 482)

    position_above = map_library_panel.MapLibraryPanel._menu_popover_position(
        button_x=760,
        button_y=550,
        button_width=28,
        button_height=28,
        root_width=800,
        root_height=600,
        menu_width=240,
        menu_height=96,
        margin=8,
        gap=4,
    )

    assert position_above == (548, 450)


def test_library_action_buttons_use_normalized_dimensions():
    assert splash_screen._LIBRARY_ACTION_BUTTON_SIZE == 32
    assert splash_screen._LIBRARY_ACTION_ICON_STROKE_WIDTH == 2
    assert splash_screen._LIBRARY_OVERFLOW_BUTTON_SIZE == 28
    assert splash_screen._TYPOGRAPHY.supporting[1] == 10
    style = splash_screen._map_library_panel_style()
    assert not hasattr(style, "scrollbar_right_inset")
    assert style.panel_border_color == splash_screen._LIBRARY_PANEL_BORDER_COLOR


def test_map_library_scroll_region_delegates_overflow_to_the_shared_rail():
    class _FakeCanvas:
        def __init__(self) -> None:
            self.height = 200
            self.configurations = []
        def winfo_width(self) -> int:
            return 320

        def configure(self, **options) -> None:
            self.configurations.append(options)

    class _FakeRows:
        def __init__(self) -> None:
            self.height = 320

        def winfo_reqheight(self) -> int:
            return self.height

    class _FakeScrollbar:
        def __init__(self) -> None:
            self.content_heights = []

        def sync_overflow(self, content_height: int) -> None:
            self.content_heights.append(content_height)

    panel = object.__new__(map_library_panel.MapLibraryPanel)
    panel._content_canvas = _FakeCanvas()
    panel._rows_frame = _FakeRows()
    panel._content_scrollbar = _FakeScrollbar()

    panel.sync_scroll_region()

    assert panel._content_canvas.configurations == [
        {"scrollregion": (0, 0, 320, 320)}
    ]
    assert panel._content_scrollbar.content_heights == [320]

    panel._rows_frame.height = 200
    panel.sync_scroll_region()

    assert panel._content_scrollbar.content_heights == [320, 200]


def test_map_library_binds_dynamic_rows_to_the_shared_scrollbar():
    class _FakeScrollbar:
        def __init__(self) -> None:
            self.targets = []

        def bind_mousewheel(self, widget) -> None:
            self.targets.append(widget)

    panel = object.__new__(map_library_panel.MapLibraryPanel)
    panel._content_scrollbar = _FakeScrollbar()
    panel._widget_exists = lambda widget: widget == "row"

    panel.bind_mousewheel_if_ready("row")
    panel.bind_mousewheel_if_ready("destroyed-row")

    assert panel._content_scrollbar.targets == ["row"]


def test_map_library_open_map_action_uses_the_existing_folder_callback(monkeypatch):
    class _FakeCanvas:
        def __init__(self, _parent, **options) -> None:
            self.options = options
            self.bindings = {}
            self.pack_calls = []
            self.config_calls = []
            self.draw_calls = []

        def bind(self, sequence, callback, add=None) -> None:
            self.bindings[sequence] = callback

        def pack(self, **options) -> None:
            self.pack_calls.append(options)

        def config(self, **options) -> None:
            self.config_calls.append(options)

        def winfo_width(self) -> int:
            return 420

        def winfo_height(self) -> int:
            return 58

        def delete(self, tag) -> None:
            self.draw_calls.append(("delete", tag))

        def create_rectangle(self, *coordinates, **options) -> None:
            self.draw_calls.append(("rectangle", coordinates, options))

        def create_line(self, *coordinates, **options) -> None:
            self.draw_calls.append(("line", coordinates, options))

        def create_text(self, *coordinates, **options) -> None:
            self.draw_calls.append(("text", coordinates, options))

        def create_image(self, *coordinates, **options) -> None:
            self.draw_calls.append(("image", coordinates, options))

    canvases = []
    monkeypatch.setattr(
        map_library_panel.tk,
        "Canvas",
        lambda *args, **kwargs: (
            canvases.append(_FakeCanvas(*args, **kwargs)) or canvases[-1]
        ),
    )
    monkeypatch.setattr(
        map_library_panel,
        "vector_icon_photo",
        lambda _widget, **_options: object(),
    )
    opened = []
    closed_menus = []
    activations = []
    wheel_targets = []
    panel = object.__new__(map_library_panel.MapLibraryPanel)
    panel._open_map_folder = lambda: opened.append(True)
    panel.close_active_menu = lambda: closed_menus.append(True)
    panel._bind_activation = lambda _widget, callback: activations.append(callback)
    panel.bind_mousewheel_if_ready = wheel_targets.append
    panel._widget_exists = lambda _widget: True
    panel._px = lambda value: int(value)
    panel._style = SimpleNamespace(
        title_font=("TkDefaultFont", 13, "bold"),
        supporting_font=("TkDefaultFont", 11),
        title_color="#f5d77d",
        metadata_color="#6f717f",
        panel_color="#101018",
        panel_border_color="#1e2028",
        button_border_color="#a77a10",
        button_hover_bg="#2a2a33",
        featured_action_bg="#202025",
        featured_action_hover_bg="#28282e",
        menu_hover_bg="#343442",
        progress_fill_color="#f0ad22",
    )

    panel._create_open_map_action(object())

    action = canvases[0]
    assert action.options["takefocus"] is True
    assert action.options["height"] == 58
    assert action.options["bg"] == "#202025"
    assert action.pack_calls == [{"anchor": "w", "fill": "x"}]
    assert wheel_targets == [action]

    action.bindings["<Enter>"](None)
    action.bindings["<Leave>"](None)
    assert action.config_calls[-2:] == [
        {"bg": "#28282e"},
        {"bg": "#202025"},
    ]

    action.draw_calls.clear()
    action.bindings["<Configure>"](None)
    assert [entry[2]["text"] for entry in action.draw_calls if entry[0] == "text"] == [
        "Open a local map",
        "Browse a cave map folder",
    ]

    activations[0]()

    assert closed_menus == [True]
    assert opened == [True]


@pytest.mark.parametrize(
    ("action_text", "show_stop_progress", "icon", "tooltip"),
    [
        ("Open", False, "chevron-right", "Open map"),
        ("Get", False, "download", "Download map"),
        ("Retry", False, "retry", "Retry download"),
        ("", True, "stop-progress", "Stop download"),
    ],
)
def test_map_library_row_actions_use_state_aware_icons(
    action_text,
    show_stop_progress,
    icon,
    tooltip,
):
    visual = map_library_panel.map_library_action_visual(
        action_text,
        show_stop_progress=show_stop_progress,
    )

    assert visual.icon == icon
    assert visual.tooltip == tooltip


def test_map_library_retry_uses_font_awesome_at_its_inset_optical_size(monkeypatch):
    class _FakeButton:
        def __init__(self) -> None:
            self.image_calls = []

        def create_image(self, *coordinates, **options) -> None:
            self.image_calls.append((coordinates, options))

    panel = object.__new__(map_library_panel.MapLibraryPanel)
    panel._px = lambda value: value
    panel._style = SimpleNamespace(action_retry_icon_diameter=18)
    button = _FakeButton()
    retry_calls = []
    photo = object()

    def capture_retry_icon(_widget, **options):
        retry_calls.append(options)
        return photo

    monkeypatch.setattr(map_library_panel, "retry_icon_photo", capture_retry_icon)

    panel._draw_retry(button, 32, 32, "#e5a11f")

    assert len(button.image_calls) == 1
    assert retry_calls == [
        {
            "image_size": (32, 32),
            "glyph_diameter": 18,
            "color": "#e5a11f",
        }
    ]
    assert button._cv_retry_photo is photo
    assert button.image_calls == [
        ((16.0, 16.0), {"image": photo, "tags": "cv_action_content"})
    ]


@pytest.mark.parametrize(
    ("pause", "center_glyph"),
    ((False, "stop"), (True, "pause")),
)
def test_map_library_progress_control_uses_one_centered_high_dpi_photo(
    monkeypatch,
    pause,
    center_glyph,
):
    class _FakeButton:
        def __init__(self) -> None:
            self.image_calls = []

        def create_image(self, *coordinates, **options) -> None:
            self.image_calls.append((coordinates, options))

    panel = object.__new__(map_library_panel.MapLibraryPanel)
    panel._px = lambda value: int(round(value * 2.5))
    panel._style = SimpleNamespace(
        action_progress_ring_diameter=22,
        action_progress_ring_stroke_width=2,
        action_stop_size=7,
        progress_track_color="#50535c",
        progress_fill_color="#e5a11f",
        button_fg="#e5a11f",
        disabled_button_fg="#6f717f",
    )
    button = _FakeButton()
    progress_calls = []
    photo = object()

    def capture_progress_control(_widget, **options):
        progress_calls.append(options)
        return photo

    monkeypatch.setattr(
        map_library_panel,
        "progress_control_photo",
        capture_progress_control,
    )

    panel._draw_action_progress(button, 80, 80, pause=pause)

    assert progress_calls == [
        {
            "image_size": 60,
            "ring_diameter": 55,
            "stroke_width": 5,
            "track_color": "#50535c",
            "fill_color": "#e5a11f",
            "start_degrees": 90,
            "extent_degrees": -2,
            "center_glyph": center_glyph,
            "center_glyph_size": 18,
            "center_glyph_color": "#e5a11f",
        }
    ]
    assert button._cv_progress_control_photo is photo
    assert button.image_calls == [
        ((40.0, 40.0), {"image": photo, "tags": "cv_action_content"})
    ]
