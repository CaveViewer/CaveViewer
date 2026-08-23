#!/usr/bin/env python3
"""Classify a release version as new or an existing-release resume."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Iterable
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
from next_release_version import normalize_release_version


def classify_release_version(candidate: str, published_tags: Iterable[str]) -> str:
    """Return ``new`` or ``resume`` and reject versions older than published."""
    normalized_candidate = normalize_release_version(candidate)
    if normalized_candidate != candidate:
        raise ValueError(
            "release version must be canonical MAJOR.MINOR.PATCH without a v prefix"
        )
    candidate_tuple = tuple(int(part) for part in candidate.split("."))

    published_versions: list[tuple[int, int, int]] = []
    for raw_tag in published_tags:
        try:
            normalized = normalize_release_version(raw_tag)
        except ValueError:
            continue
        published_versions.append(
            tuple(int(part) for part in normalized.split("."))
        )

    if not published_versions:
        return "new"
    greatest = max(published_versions)
    if candidate_tuple > greatest:
        return "new"
    if candidate_tuple == greatest:
        return "resume"
    greatest_text = ".".join(str(part) for part in greatest)
    raise ValueError(
        f"release version {candidate} is older than published version {greatest_text}"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--candidate", required=True)
    args = parser.parse_args(argv)
    try:
        print(classify_release_version(args.candidate, sys.stdin.read().splitlines()))
    except ValueError as error:
        parser.error(str(error))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
