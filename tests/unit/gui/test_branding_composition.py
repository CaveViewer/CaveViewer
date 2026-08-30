"""Verify one semantic branding snapshot reaches GUI presentation consumers."""

from __future__ import annotations

from pathlib import Path

from caveviewer.branding import BrandingAssets, LoadingRingTokens
from caveviewer.gui import splash_screen, viewer_window
from caveviewer.gui.platform.presentation import select_presentation_profile
from caveviewer.gui.platform.runtime import create_platform_runtime


def _assets(tmp_path: Path) -> BrandingAssets:
    paths = {
        name: tmp_path / f"{name}.png"
        for name in (
            "application",
            "about",
            "loading",
            "loading_mask",
            "windows",
            "macos",
            "linux",
        )
    }
    for path in paths.values():
        path.write_bytes(b"asset")
    return BrandingAssets(
        profile_id="candidate",
        application_mark=paths["application"],
        about_mark=paths["about"],
        loading_mark=paths["loading"],
        loading_progress_mask=paths["loading_mask"],
        windows_app_icon=paths["windows"],
        macos_app_icon=paths["macos"],
        linux_app_icon=paths["linux"],
        loading_ring=LoadingRingTokens("#FFB000", "#3B3428"),
    )


def test_platform_runtime_retains_one_injected_branding_snapshot(tmp_path):
    branding_assets = _assets(tmp_path)

    runtime = create_platform_runtime(
        branding_assets=branding_assets,
        desktop_services=object(),
        environment={},
        platform_name="win32",
        machine="AMD64",
    )

    assert runtime.branding_assets is branding_assets
    assert viewer_window._runtime_app_icon_path(runtime) == str(
        branding_assets.windows_app_icon
    )


def test_splash_activates_about_and_platform_icon_roles(monkeypatch, tmp_path):
    branding_assets = _assets(tmp_path)
    profile = select_presentation_profile(platform_name="darwin")
    monkeypatch.setattr(splash_screen, "_LOGO_PATH", None)
    monkeypatch.setattr(splash_screen, "_APP_ICON_PATH", None)

    splash_screen._activate_presentation_profile(
        profile,
        branding_assets=branding_assets,
        platform_name="darwin",
    )

    assert splash_screen._LOGO_PATH == str(branding_assets.about_mark)
    assert splash_screen._APP_ICON_PATH == str(branding_assets.macos_app_icon)


def test_splash_preserves_legacy_app_icon_override(monkeypatch, tmp_path):
    branding_assets = _assets(tmp_path)
    override = tmp_path / "legacy-override.png"
    profile = select_presentation_profile(platform_name="win32")
    monkeypatch.setattr(splash_screen, "_LOGO_PATH", None)
    monkeypatch.setattr(splash_screen, "_APP_ICON_PATH", None)

    splash_screen._activate_presentation_profile(
        profile,
        branding_assets=branding_assets,
        app_icon_path_override=str(override),
        platform_name="win32",
    )

    assert splash_screen._APP_ICON_PATH == str(override)
    assert splash_screen._LOGO_PATH == str(branding_assets.about_mark)


def test_gui_consumers_do_not_name_concrete_brand_files():
    source_root = Path(viewer_window.__file__).resolve().parents[1]
    consumer_paths = (
        source_root / "gui" / "splash_screen.py",
        source_root / "gui" / "viewer_window.py",
        source_root / "gui" / "import_progress_panel.py",
        source_root / "gui" / "platform" / "presentation.py",
    )

    for path in consumer_paths:
        source = path.read_text(encoding="utf-8")
        assert "app_mark_transparent.png" not in source
        assert "app_icon_windows.png" not in source
        assert "app_icon_macos.png" not in source
        assert "app_icon_resource_name" not in source
