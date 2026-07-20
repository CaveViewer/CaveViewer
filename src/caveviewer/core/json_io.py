"""Bounded JSON file loading for core-owned input files."""

from __future__ import annotations

import json
import os
from os import PathLike


def load_bounded_json(
    path: str | PathLike[str],
    *,
    max_bytes: int,
    description: str,
):
    """Load one UTF-8 JSON file after enforcing a hard byte limit."""
    limit = max(1, int(max_bytes))
    path_text = os.fspath(path)
    file_size = os.path.getsize(path_text)
    if file_size > limit:
        raise ValueError(
            f"{description} {path_text} is {file_size} bytes, above the "
            f"{limit} byte safety limit"
        )

    with open(path_text, "rb") as file_obj:
        payload = file_obj.read(limit + 1)
    if len(payload) > limit:
        raise ValueError(
            f"{description} {path_text} grew beyond the {limit} byte safety limit"
        )

    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"{description} {path_text} is not valid UTF-8") from exc
    return json.loads(text)
