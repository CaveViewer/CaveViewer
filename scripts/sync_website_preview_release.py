#!/usr/bin/env python3
"""Render Home's static download chooser from its one release-data manifest."""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
from pathlib import Path
from typing import Any


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
RELEASE_DATA_PATH = REPOSITORY_ROOT / "website-preview/assets/data/release.json"
INDEX_PATH = REPOSITORY_ROOT / "website-preview/index.html"
START_MARKER = "<!-- release-picker:start -->"
END_MARKER = "<!-- release-picker:end -->"


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
    for key in ("product", "repository", "channel", "version"):
        _require_text(release, key, "release")

    chooser = _require_mapping(release.get("chooser"), "release.chooser")
    for key in (
        "other_platforms_label",
        "title",
        "unknown_primary_label",
        "unknown_install_note",
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

    if len(artifacts) != 4:
        raise ValueError("release artifacts must be unique")
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
    dialog_note = chooser["dialog_note"].format(version=release["version"])

    return "\n".join(
        (
            "<!-- Generated from assets/data/release.json by "
            "scripts/sync_website_preview_release.py. -->",
            '<script type="application/json" data-release-data>',
            release_json,
            "</script>",
            f'<a class="platform-download__primary" href="{_attribute(windows_url)}" data-primary-download>',
            "    <span>",
            f'        <strong data-primary-label>{_text(windows["primary_label"])}</strong>',
            f'        <small data-primary-detail>{_text(release["channel"])} {_text(release["version"])} · {_text(windows["detail"])}</small>',
            "    </span>",
            '    <b aria-hidden="true">↓</b>',
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
            f'            <span><strong>{_text(windows["label"])}</strong><small>{_text(windows["detail"])}</small></span><b aria-hidden="true">↓</b>',
            "        </a>",
            "",
            '        <div class="platform-download__mac-option">',
            '            <button type="button" aria-expanded="false" aria-controls="mac-download-options" data-mac-download-toggle data-release-platform="macos">',
            f'                <span><strong>{_text(macos["label"])}</strong><small>{_text(macos["detail"])}</small></span><b aria-hidden="true">›</b>',
            "            </button>",
            '            <div class="platform-download__mac-choices" id="mac-download-options" data-mac-download-options hidden>',
            f'                <a href="{_attribute(arm64_url)}" data-release-platform="macos-arm64"><strong>{_text(mac_architectures["arm64"]["label"])}</strong><small>{_text(mac_architectures["arm64"]["detail"])}</small></a>',
            f'                <a href="{_attribute(x86_64_url)}" data-release-platform="macos-x86_64"><strong>{_text(mac_architectures["x86_64"]["label"])}</strong><small>{_text(mac_architectures["x86_64"]["detail"])}</small></a>',
            f'                <p>{_text(macos["help"])}</p>',
            "            </div>",
            "        </div>",
            "",
            f'        <a href="{_attribute(linux_url)}" data-release-platform="linux">',
            f'            <span><strong>{_text(linux["label"])}</strong><small>{_text(linux["detail"])}</small></span><b aria-hidden="true">↓</b>',
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="fail if index.html is not generated from assets/data/release.json",
    )
    args = parser.parse_args(argv)

    try:
        release = load_release_data()
        current = INDEX_PATH.read_text(encoding="utf-8")
        rendered = render_index(current, release)
    except ValueError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2

    if current == rendered:
        return 0
    if args.check:
        print(
            "website-preview/index.html is out of date; run "
            "scripts/sync_website_preview_release.py",
            file=sys.stderr,
        )
        return 1

    INDEX_PATH.write_text(rendered, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
