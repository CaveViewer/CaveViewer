#!/usr/bin/env python3
"""Choose an explicitly bumped product version from dotted candidates."""

from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Iterable
from typing import Literal


_VERSION_PATTERN = re.compile(r"^v?(\d+(?:\.\d+)+)$")
BumpMode = Literal["patch", "minor", "major"]
_BUMP_MODES: tuple[BumpMode, ...] = ("patch", "minor", "major")


def normalize_release_version(value: str) -> str:
    """Return a canonical three-component product version."""
    match = _VERSION_PATTERN.fullmatch(str(value).strip())
    if match is None:
        raise ValueError(f"invalid dotted numeric release version: {value!r}")
    numeric_version = tuple(int(part) for part in match.group(1).split("."))
    if len(numeric_version) == 2:
        numeric_version = (*numeric_version, 0)
    elif len(numeric_version) != 3:
        raise ValueError("release versions must contain two or three components")
    return ".".join(str(part) for part in numeric_version)


def next_release_version(
    candidates: Iterable[str], bump: BumpMode = "patch"
) -> str:
    """Apply ``bump`` to the greatest valid dotted product version."""
    if bump not in _BUMP_MODES:
        raise ValueError(f"unsupported release version bump: {bump!r}")
    parsed_versions: list[tuple[tuple[int, ...], tuple[str, ...]]] = []
    for candidate in candidates:
        match = _VERSION_PATTERN.fullmatch(str(candidate).strip())
        if match is None:
            continue
        components = tuple(match.group(1).split("."))
        parsed_versions.append((tuple(int(part) for part in components), components))
    if not parsed_versions:
        raise ValueError("no valid dotted release versions were provided")

    numeric_version, _text_components = max(
        parsed_versions, key=lambda item: item[0]
    )
    if len(numeric_version) == 2:
        numeric_version = (*numeric_version, 0)
    elif len(numeric_version) != 3:
        raise ValueError(
            "the greatest release version must contain two or three components"
        )

    major, minor, patch = numeric_version
    if bump == "patch":
        patch += 1
    elif bump == "minor":
        minor += 1
        patch = 0
    else:
        major += 1
        minor = 0
        patch = 0
    return f"{major}.{minor}.{patch}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Read release-version candidates from stdin and print the selected "
            "next product version. Invalid or nonnumeric tags are ignored."
        )
    )
    parser.add_argument("--bump", choices=_BUMP_MODES, default="patch")
    args = parser.parse_args(argv)
    try:
        print(next_release_version(sys.stdin.read().splitlines(), args.bump))
    except ValueError as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
