#!/usr/bin/env python3
"""Verify the immutable release channel declared by package metadata."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Verify package metadata against the selected release channel."
    )
    parser.add_argument("--metadata-file", type=Path, required=True)
    parser.add_argument(
        "--expected-release-channel",
        choices=("stable", "prerelease"),
        required=True,
    )
    args = parser.parse_args()

    try:
        payload = json.loads(args.metadata_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise SystemExit(
            f"Error: unable to read package metadata: {args.metadata_file}"
        ) from error
    if not isinstance(payload, dict):
        raise SystemExit(
            f"Error: package metadata must be a JSON object: {args.metadata_file}"
        )
    actual_channel = payload.get("release_channel")
    if actual_channel != args.expected_release_channel:
        raise SystemExit(
            "Error: package metadata release_channel does not match the selected "
            f"release channel: {actual_channel!r} (expected "
            f"{args.expected_release_channel!r})"
        )
    print(
        "Verified package release channel: "
        f"{args.metadata_file} ({args.expected_release_channel})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
