"""Repository contracts for CaveViewer and packaged license declarations."""

from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
AGPL_NAME = "GNU Affero General Public License"
AGPL_SPDX = "AGPL-3.0-only"


def _read(relative_path: str) -> str:
    return (REPOSITORY_ROOT / relative_path).read_text(encoding="utf-8")


def test_authoritative_license_is_unmodified_agpl_version_3() -> None:
    license_text = _read("LICENSE")

    assert license_text.lstrip().startswith(
        "GNU AFFERO GENERAL PUBLIC LICENSE\n"
        "                       Version 3, 19 November 2007"
    )
    assert '"This License" refers to version 3 of the GNU Affero General Public License.' in license_text
    assert "13. Remote Network Interaction; Use with the GNU General Public License." in license_text
    assert "END OF TERMS AND CONDITIONS" in license_text


def test_first_party_metadata_uses_agpl_3_only() -> None:
    assert f'license = "{AGPL_SPDX}"' in _read("pyproject.toml")
    assert f"<project_license>{AGPL_SPDX}</project_license>" in _read(
        "packaging/linux/io.github.caveviewer.caveviewer.metainfo.xml"
    )
    assert "<metadata_license>CC0-1.0</metadata_license>" in _read(
        "packaging/linux/io.github.caveviewer.caveviewer.metainfo.xml"
    )


def test_first_party_documentation_and_ui_name_agpl() -> None:
    expected_ui_text = "Licensed under GNU AGPLv3-only."
    expected_documentation_text = f"{AGPL_NAME} version 3.0 only"

    for relative_path in (
        "README.md",
        "THIRD_PARTY_NOTICES.md",
        "docs/development/licensing.md",
        "docs/development/source-setup.md",
    ):
        normalized = " ".join(_read(relative_path).split())
        assert expected_documentation_text in normalized, relative_path

    assert expected_ui_text in _read("src/caveviewer/gui/splash_screen.py")
    assert expected_ui_text in _read(
        "src/caveviewer/gui/platform/presentation_actions.py"
    )
    windows_setup = _read("scripts/windows/setup.ps1")
    assert (
        "GNU AGPLv3-only. LICENSE and THIRD_PARTY_NOTICES.md are included."
        in windows_setup
    )
    assert f"licensed under the {AGPL_NAME} v3.0 only." in windows_setup


def test_every_platform_package_preserves_license_and_notices() -> None:
    pyinstaller_spec = _read("packaging/pyinstaller/CaveViewer.spec")
    linux_builder = _read("scripts/linux/common/build.sh")
    windows_builder = _read("scripts/windows/build.sh")
    macos_packager = _read("scripts/macos/package_macos_dmg.sh")
    macos_smoke = _read("scripts/macos/smoke_dmg.sh")
    source_packager = _read("scripts/common/package_source.sh")

    for filename in ("LICENSE", "THIRD_PARTY_NOTICES.md"):
        assert f"project_root / '{filename}'" in pyinstaller_spec
        assert f'"$repo_root/{filename}:."' in linux_builder
        assert filename in windows_builder
        assert filename in macos_packager
        assert filename in macos_smoke
        assert filename in source_packager


def test_third_party_notices_retain_separate_license_terms() -> None:
    notices = _read("THIRD_PARTY_NOTICES.md")
    retry_svg = _read("src/caveviewer/resources/images/ui/retry.svg")

    assert "MIT License" in notices
    assert "zlib/libpng license" in notices
    assert "SIL Open Font License 1.1" in notices
    assert "Font Awesome Free 7.3.1" in notices
    assert "arrow-rotate-right" in notices
    assert "CC BY 4.0" in notices
    assert "Font Awesome Free 7.3.1" in retry_svg
    assert "License - https://fontawesome.com/license/free" in retry_svg
    assert "third-party" in notices.lower()
