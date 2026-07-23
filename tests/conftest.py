"""Provide shared pytest isolation for preferences, environment, and networking."""

from __future__ import annotations

import os
import socket

import pytest


@pytest.fixture(autouse=True)
def isolated_user_environment(tmp_path, monkeypatch):
    """Keep tests away from real preferences, recordings, and env settings."""
    home = tmp_path / "home"
    home.mkdir()
    config_home = tmp_path / "config"
    config_home.mkdir()
    app_data = tmp_path / "appdata"
    app_data.mkdir()

    for key in list(os.environ):
        if key.startswith("CAVEVIEWER_"):
            monkeypatch.delenv(key, raising=False)

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    monkeypatch.setenv("APPDATA", str(app_data))
    monkeypatch.setenv("LOCALAPPDATA", str(app_data))


@pytest.fixture(autouse=True)
def block_uncontrolled_network(monkeypatch):
    """Make accidental live-network use fail immediately and visibly."""

    def blocked_connect(*_args, **_kwargs):
        raise AssertionError("Tests must not access the live network")

    monkeypatch.setattr(socket.socket, "connect", blocked_connect)


@pytest.fixture
def valid_preferences(tmp_path):
    from caveviewer.gui.preferences import preference_defaults

    values = preference_defaults()
    values["recording_dir"] = str(tmp_path / "recordings")
    values["map_library_dir"] = str(tmp_path / "downloads")
    return values
