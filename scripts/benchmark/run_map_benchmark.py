#!/usr/bin/env python3
"""Compatibility wrapper for the packaged map benchmark runner."""

from __future__ import annotations

from pathlib import Path
import sys


_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
_SOURCE_ROOT = _REPOSITORY_ROOT / "src"
if str(_SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SOURCE_ROOT))

from caveviewer.benchmarking.map_runner import main, run  # noqa: E402


if __name__ == "__main__":
    run()
