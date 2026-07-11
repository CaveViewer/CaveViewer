"""Resolve CaveViewer's persistent directories without importing GUI code."""

from __future__ import annotations

import os
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


APPLICATION_DIRECTORY_NAME = "caveviewer"
STORAGE_HOME_ENV_VAR = "CAVEVIEWER_HOME"


class StoragePathError(ValueError):
    """A configured storage path would resolve ambiguously or unsafely."""


@dataclass(frozen=True)
class ApplicationPaths:
    """Resolved configuration, data, cache, state, and runtime roots."""

    config_dir: Path
    data_dir: Path
    cache_dir: Path
    state_dir: Path
    runtime_dir: Path

    def ensure_persistent_directories(self) -> None:
        """Create persistent roots only when a caller is ready to write."""
        for path in {self.config_dir, self.data_dir, self.cache_dir, self.state_dir}:
            path.mkdir(parents=True, exist_ok=True)


def resolve_application_paths(
    *,
    environ: Mapping[str, str] | None = None,
    home: str | os.PathLike[str] | None = None,
    platform_name: str | None = None,
) -> ApplicationPaths:
    """Resolve application paths for the supplied environment and platform."""
    environment = os.environ if environ is None else environ
    platform_name = sys.platform if platform_name is None else platform_name
    home_path = Path(home) if home is not None else Path(os.path.expanduser("~"))

    override = environment.get(STORAGE_HOME_ENV_VAR, "").strip()
    if override:
        override_path = _required_absolute_path(STORAGE_HOME_ENV_VAR, override)
        return ApplicationPaths(
            config_dir=override_path / "config",
            data_dir=override_path / "data",
            cache_dir=override_path / "cache",
            state_dir=override_path / "state",
            runtime_dir=override_path / "runtime",
        )

    if platform_name.startswith("linux"):
        config_home = _xdg_path(environment, "XDG_CONFIG_HOME", home_path / ".config")
        data_home = _xdg_path(
            environment, "XDG_DATA_HOME", home_path / ".local" / "share"
        )
        cache_home = _xdg_path(environment, "XDG_CACHE_HOME", home_path / ".cache")
        state_home = _xdg_path(
            environment, "XDG_STATE_HOME", home_path / ".local" / "state"
        )
        runtime_fallback = Path(tempfile.gettempdir()) / f"caveviewer-{os.getuid()}"
        runtime_home = _xdg_path(
            environment, "XDG_RUNTIME_DIR", runtime_fallback
        )
        return ApplicationPaths(
            config_dir=config_home / APPLICATION_DIRECTORY_NAME,
            data_dir=data_home / APPLICATION_DIRECTORY_NAME,
            cache_dir=cache_home / APPLICATION_DIRECTORY_NAME,
            state_dir=state_home / APPLICATION_DIRECTORY_NAME,
            runtime_dir=runtime_home / APPLICATION_DIRECTORY_NAME,
        )

    # Preserve the historical location on macOS, Windows, and unsupported
    # platforms until their storage conventions are migrated separately.
    legacy_root = home_path / ".caveviewer"
    return ApplicationPaths(
        config_dir=legacy_root,
        data_dir=legacy_root,
        cache_dir=legacy_root,
        state_dir=legacy_root,
        runtime_dir=legacy_root / "runtime",
    )


def _xdg_path(
    environment: Mapping[str, str], variable: str, fallback: Path
) -> Path:
    raw_value = environment.get(variable, "").strip()
    # The XDG specification requires absolute values; relative settings are
    # ignored rather than being resolved against an unpredictable cwd.
    if not raw_value or not os.path.isabs(raw_value):
        return fallback
    return Path(raw_value)


def _required_absolute_path(variable: str, raw_value: str) -> Path:
    expanded = os.path.expanduser(raw_value)
    if not os.path.isabs(expanded):
        raise StoragePathError(f"{variable} must be an absolute path: {raw_value!r}")
    return Path(expanded)
