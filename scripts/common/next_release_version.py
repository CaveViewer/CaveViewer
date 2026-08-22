#!/usr/bin/env python3
"""Choose the next dotted patch version from newline-delimited candidates."""

from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Iterable


_VERSION_PATTERN = re.compile(r"^v?(\d+(?:\.\d+)+)$")


def next_release_version(candidates: Iterable[str]) -> str:
    """Increment the final component of the greatest valid dotted version."""
    parsed_versions: list[tuple[tuple[int, ...], tuple[str, ...]]] = []
    for candidate in candidates:
        match = _VERSION_PATTERN.fullmatch(str(candidate).strip())
        if match is None:
            continue
        components = tuple(match.group(1).split("."))
        parsed_versions.append((tuple(int(part) for part in components), components))
    if not parsed_versions:
        raise ValueError("no valid dotted release versions were provided")

    numeric_version, text_components = max(parsed_versions, key=lambda item: item[0])
    next_components = list(text_components)
    next_components[-1] = str(numeric_version[-1] + 1)
    return ".".join(next_components)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Read release-version candidates from stdin and print the next patch "
            "version. Invalid or nonnumeric tags are ignored."
        )
    )
    parser.parse_args(argv)
    try:
        print(next_release_version(sys.stdin.read().splitlines()))
    except ValueError as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
