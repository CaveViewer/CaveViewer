#!/usr/bin/env python3
"""Render static download and installation guidance from release data."""

from __future__ import annotations

import argparse
import copy
from datetime import date
import html
import json
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any


WEBSITE_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = WEBSITE_ROOT.parent
RELEASE_DATA_PATH = WEBSITE_ROOT / "assets/data/release.json"
INDEX_PATH = WEBSITE_ROOT / "index.html"
DOCS_PATH = WEBSITE_ROOT / "docs.html"
APPSTREAM_PATH = (
    REPOSITORY_ROOT
    / "packaging/linux/io.github.caveviewer.caveviewer.metainfo.xml"
)
START_MARKER = "<!-- release-picker:start -->"
END_MARKER = "<!-- release-picker:end -->"
DOCS_START_MARKER = "<!-- installation-guidance:start -->"
DOCS_END_MARKER = "<!-- installation-guidance:end -->"
OFFICIAL_RELEASE_REPOSITORY = "https://github.com/CaveViewer/CaveViewer"
RELEASE_VERSION_PATTERN = re.compile(r"\d+\.\d+\.\d+")
RELEASE_CHANNELS = {"Preview", "Stable"}
STABLE_MANIFEST_SOURCES = (
    (
        "Windows",
        Path("updates/windows/stable.json"),
        "CaveViewer-{version}-windows.exe",
    ),
    (
        "Linux x86_64",
        Path("updates/linux/x86_64/stable.json"),
        "CaveViewer-{version}-x86_64.AppImage",
    ),
    (
        "macOS Apple silicon",
        Path("updates/macos/arm64/stable.json"),
        "CaveViewer-{version}-macos-arm64.dmg",
    ),
    (
        "macOS Intel",
        Path("updates/macos/x86_64/stable.json"),
        "CaveViewer-{version}-macos-x86_64.dmg",
    ),
)


def _text(value: object) -> str:
    return html.escape(str(value))


def _attribute(value: object) -> str:
    return html.escape(str(value), quote=True)


def _release_url(release: dict[str, Any], artifact: str) -> str:
    return (
        f"{release['repository']}/releases/download/v{release['version']}/{artifact}"
    )


def _require_mapping(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    return value


def _require_text(mapping: dict[str, Any], key: str, label: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label}.{key} must be a non-empty string")
    return value


def _validate_release_data(release: object) -> dict[str, Any]:
    """Validate the presentation data shared by source and built site files."""

    release = _require_mapping(release, "release")
    if release.get("schema_version") != 1:
        raise ValueError("release.schema_version must be 1")
    product = _require_text(release, "product", "release")
    repository = _require_text(release, "repository", "release")
    channel = _require_text(release, "channel", "release")
    version = _require_text(release, "version", "release")
    release_date = _require_text(release, "release_date", "release")
    if product != "CaveViewer":
        raise ValueError("release.product must be CaveViewer")
    if repository != OFFICIAL_RELEASE_REPOSITORY:
        raise ValueError(
            "release.repository must be the official CaveViewer GitHub repository"
        )
    if channel not in RELEASE_CHANNELS:
        raise ValueError("release.channel must be Preview or Stable")
    if not RELEASE_VERSION_PATTERN.fullmatch(version):
        raise ValueError("release.version must contain exactly three decimal components")
    try:
        date.fromisoformat(release_date)
    except ValueError as error:
        raise ValueError("release.release_date must be a valid ISO date") from error

    chooser = _require_mapping(release.get("chooser"), "release.chooser")
    for key in (
        "other_platforms_label",
        "title",
        "unknown_primary_label",
        "dialog_note",
    ):
        _require_text(chooser, key, "release.chooser")

    platforms = _require_mapping(release.get("platforms"), "release.platforms")
    if set(platforms) != {"windows", "macos", "linux"}:
        raise ValueError("release.platforms must contain windows, macos, and linux")

    artifacts: set[str] = set()
    for name in ("windows", "linux"):
        platform = _require_mapping(platforms[name], f"release.platforms.{name}")
        for key in ("label", "primary_label", "detail", "artifact", "install_note"):
            value = _require_text(platform, key, f"release.platforms.{name}")
            if key == "artifact":
                artifacts.add(value)

    for name in ("windows", "macos"):
        platform = platforms[name]
        security = _require_mapping(
            platform.get("security"), f"release.platforms.{name}.security"
        )
        for key in ("explanation", "action"):
            _require_text(security, key, f"release.platforms.{name}.security")

    macos = _require_mapping(platforms["macos"], "release.platforms.macos")
    for key in ("label", "primary_label", "detail", "install_note", "help"):
        _require_text(macos, key, "release.platforms.macos")
    architectures = _require_mapping(
        macos.get("architectures"), "release.platforms.macos.architectures"
    )
    if set(architectures) != {"arm64", "x86_64"}:
        raise ValueError("macOS architectures must contain arm64 and x86_64")
    for architecture in architectures.values():
        architecture = _require_mapping(
            architecture, "release.platforms.macos.architectures entry"
        )
        for key in ("label", "detail", "artifact"):
            value = _require_text(
                architecture, key, "release.platforms.macos.architectures entry"
            )
            if key == "artifact":
                artifacts.add(value)

    expected_artifacts = {
        f"CaveViewer-{version}-windows.exe",
        f"CaveViewer-{version}-x86_64.AppImage",
        f"CaveViewer-{version}-macos-arm64.dmg",
        f"CaveViewer-{version}-macos-x86_64.dmg",
    }
    if artifacts != expected_artifacts:
        raise ValueError(
            "release artifacts must be the four official, version-matched "
            "CaveViewer packages"
        )
    return release


def load_release_data(path: Path = RELEASE_DATA_PATH) -> dict[str, Any]:
    """Load and validate the small static manifest before rendering it."""

    try:
        release = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"Unable to load {path}: {error}") from error
    return _validate_release_data(release)


def _expected_artifacts(version: str) -> dict[str, str]:
    return {
        "windows": f"CaveViewer-{version}-windows.exe",
        "linux": f"CaveViewer-{version}-x86_64.AppImage",
        "macos-arm64": f"CaveViewer-{version}-macos-arm64.dmg",
        "macos-x86_64": f"CaveViewer-{version}-macos-x86_64.dmg",
    }


def _published_stable_version(repository_root: Path) -> str:
    """Return the one Stable version advertised by all release manifests."""

    versions: set[str] = set()
    for label, relative_path, artifact_pattern in STABLE_MANIFEST_SOURCES:
        path = repository_root / relative_path
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ValueError(f"Unable to load {path}: {error}") from error
        manifest = _require_mapping(manifest, f"{label} Stable manifest")
        version = manifest.get("latest_version")
        if (
            not isinstance(version, str)
            or not RELEASE_VERSION_PATTERN.fullmatch(version)
        ):
            raise ValueError(
                f"{label} Stable manifest must contain a three-component latest_version"
            )
        if manifest.get("release_channel") != "stable":
            raise ValueError(f"{label} manifest is not a Stable release")
        artifact = artifact_pattern.format(version=version)
        expected_url = (
            f"{OFFICIAL_RELEASE_REPOSITORY}/releases/download/v{version}/{artifact}"
        )
        if manifest.get("download_url") != expected_url:
            raise ValueError(
                f"{label} Stable manifest does not advertise its canonical {artifact}"
            )
        versions.add(version)

    if len(versions) != 1:
        advertised = ", ".join(sorted(versions)) or "none"
        raise ValueError(
            "Stable manifests must advertise one matching version; "
            f"found {advertised}"
        )
    return versions.pop()


def _published_stable_date(repository_root: Path, version: str) -> str:
    """Read the matching AppStream entry rather than a newer Preview entry."""

    path = repository_root / APPSTREAM_PATH.relative_to(REPOSITORY_ROOT)
    try:
        root = ET.parse(path).getroot()
    except (OSError, ET.ParseError) as error:
        raise ValueError(f"Unable to load {path}: {error}") from error

    for entry in root.iter():
        if entry.tag.rsplit("}", 1)[-1] != "release":
            continue
        if entry.attrib.get("version") != version:
            continue
        release_date = entry.attrib.get("date", "")
        try:
            date.fromisoformat(release_date)
        except ValueError as error:
            raise ValueError(
                f"AppStream Stable release {version} must have a valid ISO date"
            ) from error
        return release_date
    raise ValueError(f"AppStream does not contain Stable release {version}")


def published_stable_release_data(
    *,
    template_path: Path = RELEASE_DATA_PATH,
    repository_root: Path = REPOSITORY_ROOT,
) -> dict[str, Any]:
    """Derive deployed download data from the reconciled Stable manifests."""

    release = copy.deepcopy(load_release_data(template_path))
    if release["channel"] != "Stable":
        raise ValueError("website release template must select the Stable channel")

    version = _published_stable_version(repository_root)
    release["version"] = version
    release["release_date"] = _published_stable_date(repository_root, version)
    artifacts = _expected_artifacts(version)
    release["platforms"]["windows"]["artifact"] = artifacts["windows"]
    release["platforms"]["linux"]["artifact"] = artifacts["linux"]
    architectures = release["platforms"]["macos"]["architectures"]
    architectures["arm64"]["artifact"] = artifacts["macos-arm64"]
    architectures["x86_64"]["artifact"] = artifacts["macos-x86_64"]
    return _validate_release_data(release)


def render_release_picker(release: dict[str, Any]) -> str:
    """Return the generated markup nested inside ``data-platform-download``."""

    chooser = release["chooser"]
    platforms = release["platforms"]
    windows = platforms["windows"]
    macos = platforms["macos"]
    linux = platforms["linux"]
    mac_architectures = macos["architectures"]
    windows_url = _release_url(release, windows["artifact"])
    linux_url = _release_url(release, linux["artifact"])
    arm64_url = _release_url(release, mac_architectures["arm64"]["artifact"])
    x86_64_url = _release_url(release, mac_architectures["x86_64"]["artifact"])
    release_json = json.dumps(release, separators=(",", ":"), sort_keys=True).replace(
        "</", "<\\/"
    )
    channel_label = f"{release['product']} {release['channel']} {release['version']}"
    release_date = date.fromisoformat(release["release_date"])
    dialog_note = chooser["dialog_note"].format(
        year=release_date.year,
        month=release_date.strftime("%B"),
        day=release_date.day,
    )
    download_glyph = (
        '<svg viewBox="0 0 24 24" width="20" height="20">'
        '<path d="M12 3v11m0 0 4-4m-4 4-4-4M5 17v3h14v-3" '
        'fill="none" stroke="currentColor" stroke-width="2" '
        'stroke-linecap="round" stroke-linejoin="round"/></svg>'
    )

    return "\n".join(
        (
            "<!-- Generated from assets/data/release.json by "
            "scripts/sync_release.py. -->",
            '<script type="application/json" data-release-data>',
            release_json,
            "</script>",
            f'<a class="platform-download__primary" href="{_attribute(windows_url)}" data-primary-download>',
            "    <span>",
            f'        <strong data-primary-label>{_text(windows["primary_label"])}</strong>',
            f'        <small data-primary-detail>{_text(release["channel"])} {_text(release["version"])} · {_text(windows["detail"])}</small>',
            "    </span>",
            f'    <b aria-hidden="true">{download_glyph}</b>',
            "</a>",
            "",
            f'<button class="platform-download__alternatives" type="button" data-platform-dialog-open>{_text(chooser["other_platforms_label"])}</button>',
            "",
            '<dialog class="platform-download__dialog" aria-labelledby="platform-download-dialog-title" data-platform-dialog>',
            '    <div class="platform-download__dialog-header">',
            f'        <div><small>{_text(channel_label)}</small><h2 id="platform-download-dialog-title">{_text(chooser["title"])}</h2></div>',
            '        <button type="button" aria-label="Close platform choices" data-platform-dialog-close>×</button>',
            "    </div>",
            "",
            '    <div class="platform-download__options">',
            f'        <a href="{_attribute(windows_url)}" data-release-platform="windows">',
            "            <span>",
            f'                <strong>{_text(windows["label"])}</strong><small>{_text(windows["detail"])}</small>',
            f'            </span><b aria-hidden="true">{download_glyph}</b>',
            "        </a>",
            "",
            '        <div class="platform-download__mac-option">',
            '            <button type="button" aria-expanded="false" aria-controls="mac-download-options" data-mac-download-toggle data-release-platform="macos">',
            "                <span>",
            f'                    <strong>{_text(macos["label"])}</strong><small>{_text(macos["detail"])}</small>',
            "                </span><b aria-hidden=\"true\">›</b>",
            "            </button>",
            '            <div class="platform-download__mac-choices" id="mac-download-options" data-mac-download-options hidden>',
            f'                <a href="{_attribute(arm64_url)}" data-release-platform="macos-arm64"><span><strong>{_text(mac_architectures["arm64"]["label"])}</strong><small>{_text(mac_architectures["arm64"]["detail"])}</small></span><b aria-hidden="true">{download_glyph}</b></a>',
            f'                <a href="{_attribute(x86_64_url)}" data-release-platform="macos-x86_64"><span><strong>{_text(mac_architectures["x86_64"]["label"])}</strong><small>{_text(mac_architectures["x86_64"]["detail"])}</small></span><b aria-hidden="true">{download_glyph}</b></a>',
            f'                <p>{_text(macos["help"])}</p>',
            "            </div>",
            "        </div>",
            "",
            f'        <a href="{_attribute(linux_url)}" data-release-platform="linux">',
            "            <span>",
            f'                <strong>{_text(linux["label"])}</strong><small>{_text(linux["detail"])}</small>',
            f'            </span><b aria-hidden="true">{download_glyph}</b>',
            "        </a>",
            "    </div>",
            "",
            f'    <p class="platform-download__dialog-note">{_text(dialog_note)}</p>',
            "</dialog>",
            "",
            '<noscript><p class="platform-download__noscript">',
            f'Direct downloads: <a href="{_attribute(windows_url)}">{_text(windows["label"])}</a>,',
            f' <a href="{_attribute(arm64_url)}">{_text(macos["label"])} {_text(mac_architectures["arm64"]["label"])}</a>,',
            f' <a href="{_attribute(x86_64_url)}">{_text(macos["label"])} {_text(mac_architectures["x86_64"]["label"])}</a>, or',
            f' <a href="{_attribute(linux_url)}">{_text(linux["label"])}</a>.',
            "</p></noscript>",
        )
    )


def render_installation_guidance(release: dict[str, Any]) -> str:
    """Return the generated platform cards for Docs' installation section."""

    platforms = release["platforms"]
    windows = platforms["windows"]
    macos = platforms["macos"]
    linux = platforms["linux"]
    windows_url = _release_url(release, windows["artifact"])
    linux_url = _release_url(release, linux["artifact"])
    mac_arm = macos["architectures"]["arm64"]
    mac_intel = macos["architectures"]["x86_64"]
    mac_arm_url = _release_url(release, mac_arm["artifact"])
    mac_intel_url = _release_url(release, mac_intel["artifact"])

    def download_link(url: str, label: str, accessible_label: str) -> str:
        glyph = (
            '<svg viewBox="0 0 24 24" aria-hidden="true">'
            '<path d="M12 3v11m0 0 4-4m-4 4-4-4M5 17v3h14v-3"/>'
            "</svg>"
        )
        return (
            f'<a href="{_attribute(url)}" aria-label="{_attribute(accessible_label)}">'
            f'<span>{_text(label)}</span>{glyph}</a>'
        )

    return "\n".join(
        (
            "<!-- Generated from assets/data/release.json by scripts/sync_release.py. -->",
            '<div class="docs-install-grid">',
            '<section class="docs-install-card" aria-labelledby="install-windows">',
            '<h3 id="install-windows">Windows</h3>',
            f'<p>{_text(windows["install_note"])} {_text(windows["security"]["explanation"])} {_text(windows["security"]["action"])}</p>',
            '<div class="docs-install-card__actions">',
            f'<p class="docs-install-card__downloads">{download_link(windows_url, "Windows", "Download CaveViewer for Windows")}</p>',
            "</div>",
            "</section>",
            '<section class="docs-install-card" aria-labelledby="install-macos">',
            '<h3 id="install-macos">macOS</h3>',
            f'<p>{_text(macos["install_note"])} {_text(macos["security"]["explanation"])} {_text(macos["security"]["action"])}</p>',
            '<div class="docs-install-card__actions">',
            f'<p class="docs-install-card__downloads">{download_link(mac_arm_url, mac_arm["label"], "Download CaveViewer for Apple silicon")}{download_link(mac_intel_url, mac_intel["label"], "Download CaveViewer for Intel Mac")}</p>',
            "</div>",
            "</section>",
            '<section class="docs-install-card" aria-labelledby="install-linux">',
            '<h3 id="install-linux">Linux</h3>',
            f'<p>{_text(linux["install_note"])}</p>',
            '<div class="docs-install-card__actions">',
            f'<p class="docs-install-card__downloads">{download_link(linux_url, "Linux", "Download CaveViewer for Linux")}</p>',
            "</div>",
            "</section>",
            "</div>",
        )
    )


def render_index(index_text: str, release: dict[str, Any]) -> str:
    """Replace the marked release-picker block without touching other markup."""

    start = index_text.find(START_MARKER)
    end = index_text.find(END_MARKER, start)
    if start < 0 or end < 0:
        raise ValueError("index.html must contain release-picker start/end markers")
    if index_text.find(START_MARKER, start + len(START_MARKER)) >= 0:
        raise ValueError("index.html must contain exactly one release-picker block")

    line_start = index_text.rfind("\n", 0, start) + 1
    container = index_text.rfind('<div class="platform-download"', 0, start)
    if container < 0:
        raise ValueError("release-picker block must be inside data-platform-download")
    container_line_start = index_text.rfind("\n", 0, container) + 1
    container_indent = re.match(
        r"[ \t]*", index_text[container_line_start:container]
    ).group(0)
    indent = f"{container_indent}    "
    body_indent = f"{indent}    "
    rendered_body = "\n".join(
        f"{body_indent}{line}" if line else "" for line in render_release_picker(release).splitlines()
    )
    replacement = f"{indent}{START_MARKER}\n{rendered_body}\n{indent}{END_MARKER}"
    end_line_end = index_text.find("\n", end)
    if end_line_end < 0:
        end_line_end = len(index_text)
    return f"{index_text[:line_start]}{replacement}{index_text[end_line_end:]}"


def render_docs(docs_text: str, release: dict[str, Any]) -> str:
    """Replace the marked Docs installation block without touching other markup."""

    start = docs_text.find(DOCS_START_MARKER)
    end = docs_text.find(DOCS_END_MARKER, start)
    if start < 0 or end < 0:
        raise ValueError(
            "docs.html must contain installation-guidance start/end markers"
        )
    if docs_text.find(DOCS_START_MARKER, start + len(DOCS_START_MARKER)) >= 0:
        raise ValueError(
            "docs.html must contain exactly one installation-guidance block"
        )

    line_start = docs_text.rfind("\n", 0, start) + 1
    indent = re.match(r"[ \t]*", docs_text[line_start:start]).group(0)
    rendered_body = "\n".join(
        f"{indent}{line}" if line else ""
        for line in render_installation_guidance(release).splitlines()
    )
    replacement = (
        f"{indent}{DOCS_START_MARKER}\n{rendered_body}\n"
        f"{indent}{DOCS_END_MARKER}"
    )
    end_line_end = docs_text.find("\n", end)
    if end_line_end < 0:
        end_line_end = len(docs_text)
    return f"{docs_text[:line_start]}{replacement}{docs_text[end_line_end:]}"


def synchronize_release_data(
    release: dict[str, Any],
    *,
    website_root: Path = WEBSITE_ROOT,
    check: bool = False,
    include_data: bool = False,
) -> bool:
    """Render one release-data document into a source tree or staging artifact.

    Return ``True`` when every target was already current. In write mode, stale
    files are updated and the return value still records that a change occurred.
    Source-template checks preserve their existing JSON formatting; staged
    builds pass ``include_data=True`` to replace the copied release document.
    """

    data_path = website_root / "assets/data/release.json"
    index_path = website_root / "index.html"
    docs_path = website_root / "docs.html"
    try:
        current_data = data_path.read_text(encoding="utf-8")
        current_index = index_path.read_text(encoding="utf-8")
        current_docs = docs_path.read_text(encoding="utf-8")
    except OSError as error:
        raise ValueError(f"Unable to load website release targets: {error}") from error

    rendered_index = render_index(current_index, release)
    rendered_docs = render_docs(current_docs, release)
    rendered_data = f"{json.dumps(release, indent=2)}\n"
    current = current_index == rendered_index and current_docs == rendered_docs
    if include_data:
        current = current and current_data == rendered_data
    if not current and not check:
        if include_data:
            data_path.write_text(rendered_data, encoding="utf-8")
        index_path.write_text(rendered_index, encoding="utf-8")
        docs_path.write_text(rendered_docs, encoding="utf-8")
    return current


def synchronize_published_stable_release(
    *,
    website_root: Path = WEBSITE_ROOT,
    repository_root: Path = REPOSITORY_ROOT,
    check: bool = False,
) -> bool:
    """Sync a staged website from Stable metadata already reconciled in main."""

    release = published_stable_release_data(
        template_path=website_root / "assets/data/release.json",
        repository_root=repository_root,
    )
    return synchronize_release_data(
        release,
        website_root=website_root,
        check=check,
        include_data=True,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="fail if generated HTML is out of date with assets/data/release.json",
    )
    parser.add_argument(
        "--published-stable",
        action="store_true",
        help=(
            "derive the staged website release data from Stable update manifests "
            "already reconciled in the repository"
        ),
    )
    parser.add_argument(
        "--website-root",
        type=Path,
        default=WEBSITE_ROOT,
        help="website source tree or staged Pages artifact to synchronize",
    )
    parser.add_argument(
        "--repository-root",
        type=Path,
        default=REPOSITORY_ROOT,
        help="repository containing Stable manifests and AppStream metadata",
    )
    args = parser.parse_args(argv)

    try:
        if args.published_stable:
            current = synchronize_published_stable_release(
                website_root=args.website_root,
                repository_root=args.repository_root,
                check=args.check,
            )
        else:
            release = load_release_data(
                args.website_root / "assets/data/release.json"
            )
            current = synchronize_release_data(
                release, website_root=args.website_root, check=args.check
            )
    except ValueError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2

    if current:
        return 0
    if args.check:
        command = (
            "scripts/sync_release.py --published-stable"
            if args.published_stable
            else "scripts/sync_release.py"
        )
        print(
            f"generated release files are out of date; run {command}",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
