"""Build non-executing Windows Explorer selection commands."""

from __future__ import annotations

import os


def explorer_select_command(path: str) -> list[str]:
    """Build Explorer arguments that preserve a whitespace-containing path.

    Explorer recognizes ``/select,`` as a switch only when it remains separate
    from the quoted target path. Keeping them as two arguments lets
    ``subprocess`` serialize a path such as ``C:\\Maps\\Devils Eye\\trace.jsonl``
    as ``explorer /select, "C:\\Maps\\Devils Eye\\trace.jsonl"``.
    """
    normalized_path = os.path.normpath(os.path.abspath(path))
    return ["explorer", "/select,", normalized_path]
