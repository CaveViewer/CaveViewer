"""Validate CaveViewer's stable Linux desktop identity and metadata."""

from __future__ import annotations

import configparser
import xml.etree.ElementTree as ElementTree
from pathlib import Path

from caveviewer.version import APPLICATION_ID, APP_VERSION


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
LINUX_PACKAGING = REPOSITORY_ROOT / "packaging" / "linux"


def test_linux_metadata_uses_one_stable_application_id():
    desktop_template = LINUX_PACKAGING / f"{APPLICATION_ID}.desktop.in"
    metainfo_path = LINUX_PACKAGING / f"{APPLICATION_ID}.metainfo.xml"

    parser = configparser.ConfigParser(interpolation=None)
    parser.optionxform = str
    parser.read_string(
        desktop_template.read_text(encoding="utf-8").replace("@EXEC@", "AppRun")
    )
    desktop = parser["Desktop Entry"]
    metainfo = ElementTree.parse(metainfo_path).getroot()

    assert APPLICATION_ID == "io.github.kernalpanic.caveviewer"
    assert desktop["Icon"] == APPLICATION_ID
    assert desktop["StartupWMClass"] == APPLICATION_ID
    assert desktop["Exec"] == "AppRun"
    assert metainfo.findtext("id") == APPLICATION_ID
    assert metainfo.find("launchable").text == f"{APPLICATION_ID}.desktop"
    assert any(
        release.attrib.get("version") == APP_VERSION
        for release in metainfo.findall("./releases/release")
    )


def test_linux_packager_installs_canonical_metadata_without_inline_duplicate():
    package_script = (
        REPOSITORY_ROOT / "scripts" / "linux" / "common" / "package.sh"
    ).read_text(encoding="utf-8")

    assert 'desktop_template="$repo_root/packaging/linux/${APPLICATION_ID}.desktop.in"' in package_script
    assert 'metainfo_src="$repo_root/packaging/linux/${APPLICATION_ID}.metainfo.xml"' in package_script
    assert 'cat > "$appdir/caveviewer.desktop"' not in package_script
    assert "\nIcon=caveviewer\n" not in package_script
