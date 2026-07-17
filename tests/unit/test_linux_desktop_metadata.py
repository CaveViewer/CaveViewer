"""Validate CaveViewer's stable Linux desktop identity and metadata."""

from __future__ import annotations

import configparser
import os
import subprocess
import xml.etree.ElementTree as ElementTree
from pathlib import Path

import pytest

from caveviewer.version import APPLICATION_ID, APP_VERSION


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
LINUX_PACKAGING = REPOSITORY_ROOT / "packaging" / "linux"
PACKAGE_SCRIPT = REPOSITORY_ROOT / "scripts" / "linux" / "common" / "package.sh"
RAW_GITHUB_MAIN_URL = "https://raw.githubusercontent.com/KernalPanic/CaveViewer/main/"
requires_executable_shell_scripts = pytest.mark.skipif(
    os.name == "nt",
    reason="Linux AppRun shell-script smoke tests are exercised on Unix CI",
)


def _generated_apprun_script() -> str:
    package_script = PACKAGE_SCRIPT.read_text(encoding="utf-8")
    marker = 'cat > "$appdir/AppRun" <<\'APP_RUN_EOF\'\n'
    start = package_script.index(marker) + len(marker)
    end = package_script.index("\nAPP_RUN_EOF", start)
    return package_script[start:end] + "\n"


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
    assert desktop["StartupNotify"] == "true"
    assert desktop["Exec"] == "AppRun %f"
    assert desktop["MimeType"] == "model/gltf-binary;model/obj;"
    assert desktop["Categories"] == "Graphics;3DGraphics;Viewer;"
    assert {
        "Cave",
        "Survey",
        "3D",
        "Map",
        "Viewer",
        "OBJ",
        "GLB",
        "Photogrammetry",
    } <= {keyword for keyword in desktop["Keywords"].split(";") if keyword}
    assert metainfo.findtext("id") == APPLICATION_ID
    assert metainfo.find("launchable").text == f"{APPLICATION_ID}.desktop"
    assert [
        mediatype.text for mediatype in metainfo.findall("./provides/mediatype")
    ] == ["model/gltf-binary", "model/obj"]
    assert any(
        release.attrib.get("version") == APP_VERSION
        for release in metainfo.findall("./releases/release")
    )

    screenshot_urls = [
        image.text for image in metainfo.findall("./screenshots/screenshot/image")
    ]
    assert screenshot_urls
    for url in screenshot_urls:
        assert url is not None
        assert url.startswith(RAW_GITHUB_MAIN_URL)
        local_path = REPOSITORY_ROOT / url.removeprefix(RAW_GITHUB_MAIN_URL)
        assert local_path.is_file(), url


def test_linux_packager_installs_canonical_metadata_without_inline_duplicate():
    package_script = PACKAGE_SCRIPT.read_text(encoding="utf-8")

    assert 'desktop_template="$repo_root/packaging/linux/${APPLICATION_ID}.desktop.in"' in package_script
    assert 'metainfo_src="$repo_root/packaging/linux/${APPLICATION_ID}.metainfo.xml"' in package_script
    assert 'metainfo_dir="$data_home/metainfo"' in package_script
    assert 'metainfo_path="$metainfo_dir/${application_id}.metainfo.xml"' in package_script
    assert 'metainfo_source="$appdir/usr/share/metainfo/${application_id}.metainfo.xml"' in package_script
    assert 'cp "$metainfo_source" "$metainfo_path"' in package_script
    assert 'Exec=\\"$escaped_appimage\\" %f' in package_script
    assert 'uninstall_only="${CAVEVIEWER_APPRUN_UNINSTALL:-0}"' in package_script
    assert 'cat > "$appdir/caveviewer.desktop"' not in package_script
    assert "\nIcon=caveviewer\n" not in package_script
    assert "xdg-mime default" not in package_script
    assert "mimeapps.list" not in package_script


@requires_executable_shell_scripts
def test_generated_apprun_install_and_uninstall_modes_manage_xdg_metadata(tmp_path):
    appdir = tmp_path / "AppDir"
    appdir.mkdir()
    applications_source = appdir / "usr" / "share" / "applications"
    metainfo_source = appdir / "usr" / "share" / "metainfo"
    icon_source = appdir / "usr" / "share" / "icons" / "hicolor" / "256x256" / "apps"
    applications_source.mkdir(parents=True)
    metainfo_source.mkdir(parents=True)
    icon_source.mkdir(parents=True)

    desktop_template = LINUX_PACKAGING / f"{APPLICATION_ID}.desktop.in"
    (applications_source / f"{APPLICATION_ID}.desktop").write_text(
        desktop_template.read_text(encoding="utf-8").replace("@EXEC@", "AppRun"),
        encoding="utf-8",
    )
    (metainfo_source / f"{APPLICATION_ID}.metainfo.xml").write_text(
        (LINUX_PACKAGING / f"{APPLICATION_ID}.metainfo.xml").read_text(
            encoding="utf-8"
        ),
        encoding="utf-8",
    )
    (icon_source / f"{APPLICATION_ID}.png").write_bytes(b"fake icon")

    apprun = appdir / "AppRun"
    apprun.write_text(_generated_apprun_script(), encoding="utf-8")
    apprun.chmod(0o755)

    xdg_data_home = tmp_path / "xdg-data"
    runtime_dir = tmp_path / "runtime"
    home = tmp_path / "home"
    appimage = tmp_path / "CaveViewer & Test.AppImage"
    runtime_dir.mkdir(mode=0o700)
    home.mkdir(exist_ok=True)
    appimage.write_bytes(b"fake appimage")

    env = os.environ.copy()
    env.update(
        {
            "APPDIR": str(appdir),
            "APPIMAGE": str(appimage),
            "HOME": str(home),
            "XDG_DATA_HOME": str(xdg_data_home),
            "XDG_RUNTIME_DIR": str(runtime_dir),
            "CAVEVIEWER_APPRUN_INSTALL_ONLY": "1",
            "CAVEVIEWER_NO_DESKTOP_INTEGRATION": "0",
        }
    )
    result = subprocess.run(
        [str(apprun)],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )

    installed_desktop = xdg_data_home / "applications" / f"{APPLICATION_ID}.desktop"
    installed_metainfo = xdg_data_home / "metainfo" / f"{APPLICATION_ID}.metainfo.xml"
    installed_icon = (
        xdg_data_home
        / "icons"
        / "hicolor"
        / "256x256"
        / "apps"
        / f"{APPLICATION_ID}.png"
    )

    assert "[CaveViewer AppRun] Desktop integration smoke mode complete." in result.stdout
    assert f"[CaveViewer AppRun] Desktop file: {installed_desktop}" in result.stdout
    assert f"[CaveViewer AppRun] AppStream metadata: {installed_metainfo}" in result.stdout
    assert installed_desktop.is_file()
    assert installed_metainfo.is_file()
    assert installed_icon.read_bytes() == b"fake icon"
    assert f'Exec="{appimage}" %f' in installed_desktop.read_text(encoding="utf-8")
    assert "MimeType=model/gltf-binary;model/obj;" in installed_desktop.read_text(
        encoding="utf-8"
    )

    unrelated_icon = (
        xdg_data_home
        / "icons"
        / "hicolor"
        / "256x256"
        / "apps"
        / "unrelated-app.png"
    )
    unrelated_icon.write_bytes(b"keep")
    legacy_desktop = xdg_data_home / "applications" / "caveviewer.desktop"
    legacy_desktop.write_text(
        "[Desktop Entry]\nName=CaveViewer\nIcon=caveviewer\n",
        encoding="utf-8",
    )

    env["CAVEVIEWER_APPRUN_INSTALL_ONLY"] = "0"
    env["CAVEVIEWER_APPRUN_UNINSTALL"] = "1"
    uninstall_result = subprocess.run(
        [str(apprun)],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )

    assert (
        "[CaveViewer AppRun] Desktop integration uninstall complete."
        in uninstall_result.stdout
    )
    assert f"[CaveViewer AppRun] Removed desktop file: {installed_desktop}" in (
        uninstall_result.stdout
    )
    assert not installed_desktop.exists()
    assert not installed_metainfo.exists()
    assert not installed_icon.exists()
    assert not legacy_desktop.exists()
    assert unrelated_icon.read_bytes() == b"keep"


@requires_executable_shell_scripts
def test_generated_apprun_does_not_override_window_system_policy(tmp_path):
    appdir = tmp_path / "AppDir"
    executable = appdir / "usr" / "lib" / "caveviewer" / "CaveViewer"
    executable.parent.mkdir(parents=True)
    executable.write_text(
        "#!/usr/bin/env sh\n"
        'echo "fake-window-system=${CAVEVIEWER_WINDOW_SYSTEM:-}"\n',
        encoding="utf-8",
    )
    executable.chmod(0o755)

    apprun = appdir / "AppRun"
    apprun.write_text(_generated_apprun_script(), encoding="utf-8")
    apprun.chmod(0o755)

    runtime_dir = tmp_path / "runtime"
    home = tmp_path / "home"
    runtime_dir.mkdir(mode=0o700)
    home.mkdir(exist_ok=True)

    env = os.environ.copy()
    env.update(
        {
            "APPDIR": str(appdir),
            "HOME": str(home),
            "XDG_RUNTIME_DIR": str(runtime_dir),
            "XDG_SESSION_TYPE": "wayland",
            "WAYLAND_DISPLAY": "wayland-0",
            "DISPLAY": ":0",
            "CAVEVIEWER_LAUNCH_DEBUG": "1",
            "CAVEVIEWER_NO_DESKTOP_INTEGRATION": "1",
            "CAVEVIEWER_TK_SCALE": "1.0",
        }
    )
    env.pop("APPIMAGE", None)
    env.pop("CAVEVIEWER_WINDOW_SYSTEM", None)

    result = subprocess.run(
        [str(apprun)],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )

    assert "[CaveViewer AppRun] CAVEVIEWER_WINDOW_SYSTEM=" in result.stdout
    assert "window_system_note=" not in result.stdout
    assert "fake-window-system=" in result.stdout

    env["CAVEVIEWER_WINDOW_SYSTEM"] = "wayland"
    explicit_result = subprocess.run(
        [str(apprun)],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )

    assert "[CaveViewer AppRun] CAVEVIEWER_WINDOW_SYSTEM=wayland" in explicit_result.stdout
    assert "window_system_note=" not in explicit_result.stdout
    assert "fake-window-system=wayland" in explicit_result.stdout


def test_ci_runs_freedesktop_metadata_validators():
    workflow = (
        REPOSITORY_ROOT / ".github" / "workflows" / "tests.yml"
    ).read_text(encoding="utf-8")

    assert "appstream desktop-file-utils" in workflow
    assert "desktop-file-validate" in workflow
    assert "appstreamcli validate --no-net --pedantic" in workflow
    assert f"packaging/linux/{APPLICATION_ID}.desktop.in" in workflow
    assert f"packaging/linux/{APPLICATION_ID}.metainfo.xml" in workflow
