"""Presentation-independent map-library row models for the splash screen."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass

from caveviewer.core.chunking.staging import MANIFEST_NAME as CACHE_MANIFEST_NAME
from caveviewer.core.map.source_model import find_model_file


_RECENT_TITLE_MANIFEST_SCAN_BYTES = 64 * 1024
_RECENT_SOURCE_OBJ_PATTERN = re.compile(
    r'"source_obj"\s*:\s*"((?:\\.|[^"\\])*)"'
)


@dataclass(frozen=True)
class RecentMapEntry:
    """Display model for one user-opened recent map."""

    path: str
    key: str
    title: str
    detail: str = ""


def recent_map_entry(path: str) -> RecentMapEntry:
    """Build the immutable display model for one recent map path."""
    return RecentMapEntry(
        path=path,
        key=recent_map_key(path),
        title=recent_map_title(path),
        detail=recent_map_detail_text(path),
    )


def recent_map_key(path: str) -> str:
    """Return a stable comparison key for a recent map path."""
    try:
        return os.path.normcase(os.path.abspath(os.path.expanduser(path)))
    except (OSError, TypeError, ValueError):
        return str(path)


def recent_map_detail_text(path: str) -> str:
    """Return secondary text for a recent user map row."""
    del path
    return ""


def recent_map_title(path: str) -> str:
    """Return the visible title for a recent user map row."""
    source_title = _recent_map_source_title(path)
    if source_title:
        return source_title
    normalized = os.path.normpath(os.path.abspath(path))
    return os.path.basename(normalized) or normalized


def _recent_map_source_title(path: str) -> str | None:
    """
    Return a user-facing source model title for a recent-map entry.

    Normal recent entries point at the original selected map folder. Older
    builds could accidentally persist the managed cache directory instead; for
    those stale entries, recover the original source filename from the cache
    manifest without reading the full potentially-large manifest.
    """
    source_path = _recent_map_source_path(path)
    if source_path:
        title = _title_from_source_name(source_path)
        if title:
            return title
    return _recent_map_cache_manifest_title(path)


def _recent_map_source_path(path: str) -> str | None:
    try:
        descriptor = find_model_file(path)
    except (FileNotFoundError, OSError, TypeError, ValueError):
        return None
    source_path = descriptor.get("obj_path") or descriptor.get("glb_path")
    return source_path if isinstance(source_path, str) else None


def _recent_map_cache_manifest_title(path: str) -> str | None:
    try:
        manifest_path = os.path.join(
            os.path.abspath(os.path.expanduser(path)),
            CACHE_MANIFEST_NAME,
        )
        if not os.path.isfile(manifest_path):
            return None
        with open(manifest_path, "rb") as file_obj:
            payload = file_obj.read(_RECENT_TITLE_MANIFEST_SCAN_BYTES)
        text = payload.decode("utf-8", errors="replace")
        match = _RECENT_SOURCE_OBJ_PATTERN.search(text)
        if not match:
            return None
        source_name = json.loads(f'"{match.group(1)}"')
    except Exception:
        return None
    if not isinstance(source_name, str):
        return None
    return _title_from_source_name(source_name)


def _title_from_source_name(source_name: str) -> str:
    basename = os.path.basename(source_name.strip())
    stem, _extension = os.path.splitext(basename)
    return (stem or basename).strip()
