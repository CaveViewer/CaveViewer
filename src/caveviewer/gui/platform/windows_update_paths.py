"""Resolve user-owned locations for Windows update artifacts and diagnostics."""

from __future__ import annotations

import os
from pathlib import Path


def default_windows_update_root() -> Path:
    """Return the per-user directory for automatic installer payloads and logs."""
    local_app_data = os.environ.get("LOCALAPPDATA", "").strip()
    if local_app_data:
        return Path(local_app_data) / "CaveViewer" / "updates"
    return Path.home() / "AppData" / "Local" / "CaveViewer" / "updates"
