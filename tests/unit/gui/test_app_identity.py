"""Tests for native-window application identity helpers."""

from __future__ import annotations

from caveviewer.gui.platform.app_identity import (
    LINUX_WINDOW_INSTANCE_NAME,
    tk_root_options,
)
from caveviewer.version import APPLICATION_ID, APP_NAME


def test_linux_tk_root_options_match_desktop_identity():
    assert tk_root_options(platform_name="linux") == {
        "baseName": LINUX_WINDOW_INSTANCE_NAME,
        "className": APPLICATION_ID,
    }


def test_non_linux_tk_root_options_keep_user_facing_name():
    assert tk_root_options(platform_name="darwin") == {
        "baseName": APP_NAME,
        "className": APP_NAME,
    }
    assert tk_root_options(platform_name="win32") == {
        "baseName": APP_NAME,
        "className": APP_NAME,
    }
