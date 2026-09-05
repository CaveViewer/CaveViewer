#!/usr/bin/env python3
"""Render static download and installation guidance from release data."""

from __future__ import annotations

import argparse
from datetime import date
import html
import json
import re
import sys
from pathlib import Path
from typing import Any


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
RELEASE_DATA_PATH = REPOSITORY_ROOT / "assets/data/release.json"
INDEX_PATH = REPOSITORY_ROOT / "index.html"
DOCS_PATH = REPOSITORY_ROOT / "docs.html"
START_MARKER = "<!-- release-picker:start -->"
END_MARKER = "<!-- release-picker:end -->"
DOCS_START_MARKER = "<!-- installation-guidance:start -->"
DOCS_END_MARKER = "<!-- installation-guidance:end -->"
OFFICIAL_RELEASE_REPOSITORY = "https://github.com/CaveViewer/CaveViewer"
RELEASE_VERSION_PATTERN = re.compile(r"\d+\.\d+\.\d+")
RELEASE_CHANNELS = {"Preview", "Stable"}


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


def load_release_data(path: Path = RELEASE_DATA_PATH) -> dict[str, Any]:
    """Load and validate the small static manifest before rendering it."""

    try:
        release = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"Unable to load {path}: {error}") from error

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
    mac_arm_accessible_label = f"Download CaveViewer for {mac_arm['label']}"
    mac_intel_accessible_label = f"Download CaveViewer for {mac_intel['label']}"

    glyph = (
        '<svg viewBox="0 0 24 24" aria-hidden="true">'
        '<path d="M12 3v11m0 0 4-4m-4 4-4-4M5 17v3h14v-3"/>'
        "</svg>"
    )

    def download_link(url: str, label: str, accessible_label: str) -> str:
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
            '<div class="docs-install-card__downloads docs-install-card__downloads--macos" data-macos-download>',
            f'    <a href="{_attribute(mac_arm_url)}" aria-label="{_attribute(mac_arm_accessible_label)}" data-macos-download-link>',
            f'        <span data-macos-download-label>{_text(mac_arm["label"])}</span>{glyph}',
            "    </a>",
            '    <label class="docs-install-card__architecture">',
            '        <span>Mac architecture</span>',
            '        <select data-macos-architecture>',
            f'            <option value="arm64" data-download-url="{_attribute(mac_arm_url)}" data-download-label="{_attribute(mac_arm["label"])}" data-download-aria-label="{_attribute(mac_arm_accessible_label)}" selected>{_text(mac_arm["label"])} ({_text(mac_arm["detail"])})</option>',
            f'            <option value="x86_64" data-download-url="{_attribute(mac_intel_url)}" data-download-label="{_attribute(mac_intel["label"])}" data-download-aria-label="{_attribute(mac_intel_accessible_label)}">{_text(mac_intel["label"])} ({_text(mac_intel["detail"])})</option>',
            "        </select>",
            "    </label>",
            "</div>",
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="fail if generated HTML is out of date with assets/data/release.json",
    )
    args = parser.parse_args(argv)

    try:
        release = load_release_data()
        current_index = INDEX_PATH.read_text(encoding="utf-8")
        current_docs = DOCS_PATH.read_text(encoding="utf-8")
        rendered_index = render_index(current_index, release)
        rendered_docs = render_docs(current_docs, release)
    except ValueError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2

    if current_index == rendered_index and current_docs == rendered_docs:
        return 0
    if args.check:
        print(
            "generated HTML is out of date; run scripts/sync_release.py",
            file=sys.stderr,
        )
        return 1

    INDEX_PATH.write_text(rendered_index, encoding="utf-8")
    DOCS_PATH.write_text(rendered_docs, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
