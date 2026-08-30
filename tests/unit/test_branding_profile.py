"""Validate semantic branding profiles and developer-only selection."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil

import pytest

from caveviewer.branding import (
    BRANDING_PROFILE_ENVIRONMENT_VARIABLE,
    REQUIRED_ROLES,
    BrandingProfileError,
    default_branding_manifest_path,
    load_branding_profile,
    resolve_branding_assets,
    resolve_branding_profile,
)


def test_bundled_default_profile_uses_original_mark_for_windows_and_about():
    profile = resolve_branding_profile(environ={}, frozen=False)

    assert profile.profile_id == "default"
    assert set(profile.roles) == REQUIRED_ROLES
    assert profile.asset_for("application_mark").width >= 1024
    assert profile.asset_for("application_mark").height >= 1024
    assert profile.asset_for("application_mark").alpha_required is True
    assert profile.asset_for("about_mark") is profile.asset_for("application_mark")
    assert profile.asset_for("application_mark").path.name == "caveviewer-mark.png"
    assert profile.asset_for("windows_app_icon").path.name == (
        "windows-small-mark.png"
    )
    assert profile.asset_for("windows_app_icon") is not profile.asset_for(
        "about_mark"
    )
    assert profile.asset_for("loading_mark").path.name == "loading-progress-mark.png"
    assert profile.asset_for("loading_progress_mask").path.name == (
        "loading-progress-rim-mask.png"
    )
    assert profile.asset_for("loading_mark") is not profile.asset_for(
        "windows_app_icon"
    )
    assert profile.loading_ring.fill_color == "#FFB000"
    assert profile.loading_ring.track_color == "#3B3428"
    assert profile.loading_ring.mode == "ring_with_mark"


def test_runtime_snapshot_resolves_semantic_paths_and_platform_icons():
    assets = resolve_branding_assets(environ={})

    assert assets.profile_id == "default"
    assert assets.about_mark.is_file()
    assert assets.loading_mark.is_file()
    assert assets.application_icon_for("win32") == assets.windows_app_icon
    assert assets.application_icon_for("darwin") == assets.macos_app_icon
    assert assets.application_icon_for("linux") == assets.linux_app_icon


def test_developer_override_accepts_a_profile_directory(tmp_path):
    profile_dir = _copy_default_profile(tmp_path)
    payload = _read_manifest(profile_dir)
    payload["profile_id"] = "candidate"
    payload["display_name"] = "Candidate brand"
    _write_manifest(profile_dir, payload)

    profile = resolve_branding_profile(
        environ={BRANDING_PROFILE_ENVIRONMENT_VARIABLE: str(profile_dir)},
        frozen=False,
    )

    assert profile.profile_id == "candidate"
    assert profile.manifest_path.parent == profile_dir.resolve()


def test_frozen_application_ignores_external_profile_override(tmp_path):
    profile_dir = _copy_default_profile(tmp_path)
    payload = _read_manifest(profile_dir)
    payload["profile_id"] = "external"
    _write_manifest(profile_dir, payload)

    profile = resolve_branding_profile(
        environ={BRANDING_PROFILE_ENVIRONMENT_VARIABLE: str(profile_dir)},
        frozen=True,
    )

    assert profile.profile_id == "default"


@pytest.mark.parametrize(
    ("mutation", "expected_error"),
    [
        (lambda payload: payload.update(schema_version=2), "schema_version"),
        (
            lambda payload: payload["roles"].pop("windows_app_icon"),
            "windows_app_icon",
        ),
        (
            lambda payload: payload["roles"].update(about_mark="missing"),
            "references unknown asset",
        ),
        (
            lambda payload: payload["assets"]["cookie"].update(path="../outside.png"),
            "must stay inside",
        ),
        (
            lambda payload: payload["assets"]["cookie"].update(safe_area_inset=0.5),
            "between 0 and 0.25",
        ),
        (
            lambda payload: payload["loading_ring"].update(mode="logo_spinner"),
            "mode must be",
        ),
    ],
)
def test_profile_rejects_invalid_contracts(tmp_path, mutation, expected_error):
    profile_dir = _copy_default_profile(tmp_path)
    payload = _read_manifest(profile_dir)
    mutation(payload)
    _write_manifest(profile_dir, payload)

    with pytest.raises(BrandingProfileError, match=expected_error):
        load_branding_profile(profile_dir / "branding.v1.json")


def test_profile_rejects_changed_artwork(tmp_path):
    profile_dir = _copy_default_profile(tmp_path)
    artwork = profile_dir / "application-mark.png"
    artwork.write_bytes(artwork.read_bytes() + b"changed")

    with pytest.raises(BrandingProfileError, match="SHA-256 mismatch"):
        load_branding_profile(profile_dir / "branding.v1.json")


def test_profile_rejects_non_square_png(tmp_path):
    profile_dir = _copy_default_profile(tmp_path)
    artwork = profile_dir / "application-mark.png"
    data = bytearray(artwork.read_bytes())
    data[20:24] = (1200).to_bytes(4, "big")
    artwork.write_bytes(data)
    payload = _read_manifest(profile_dir)
    payload["assets"]["cookie"]["sha256"] = hashlib.sha256(data).hexdigest()
    _write_manifest(profile_dir, payload)

    with pytest.raises(BrandingProfileError, match="must be square"):
        load_branding_profile(profile_dir / "branding.v1.json")


def test_profile_rejects_png_without_required_alpha(tmp_path):
    profile_dir = _copy_default_profile(tmp_path)
    artwork = profile_dir / "application-mark.png"
    data = bytearray(artwork.read_bytes())
    data[25] = 2
    artwork.write_bytes(data)
    payload = _read_manifest(profile_dir)
    payload["assets"]["cookie"]["sha256"] = hashlib.sha256(data).hexdigest()
    _write_manifest(profile_dir, payload)

    with pytest.raises(BrandingProfileError, match="requires an alpha channel"):
        load_branding_profile(profile_dir / "branding.v1.json")


def _copy_default_profile(tmp_path: Path) -> Path:
    profile_dir = tmp_path / "profile"
    profile_dir.mkdir()
    manifest = default_branding_manifest_path()
    shutil.copy2(manifest, profile_dir / manifest.name)
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    for asset in payload["assets"].values():
        source = manifest.parent / asset["path"]
        shutil.copy2(source, profile_dir / source.name)
    return profile_dir


def _read_manifest(profile_dir: Path) -> dict:
    return json.loads((profile_dir / "branding.v1.json").read_text(encoding="utf-8"))


def _write_manifest(profile_dir: Path, payload: dict) -> None:
    (profile_dir / "branding.v1.json").write_text(
        json.dumps(payload), encoding="utf-8"
    )
