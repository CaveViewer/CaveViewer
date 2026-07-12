"""Platform-specific native-window identity helpers."""

from __future__ import annotations

import sys

from caveviewer.version import APPLICATION_ID, APP_NAME

LINUX_WINDOW_INSTANCE_NAME = "caveviewer"


def tk_root_options(*, platform_name: str | None = None) -> dict[str, str]:
    """Return the Tk identity options used when creating application roots."""
    platform_name = sys.platform if platform_name is None else platform_name
    if platform_name.startswith("linux"):
        return {
            "baseName": LINUX_WINDOW_INSTANCE_NAME,
            "className": APPLICATION_ID,
        }
    return {
        "baseName": APP_NAME,
        "className": APP_NAME,
    }
