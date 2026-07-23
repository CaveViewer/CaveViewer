"""Resolve CaveViewer's persistent directories without importing GUI code."""

from __future__ import annotations

import os
import stat
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

    home_path = _required_home_path(home)

    if platform_name.startswith("linux"):
        config_home = _xdg_path(environment, "XDG_CONFIG_HOME", home_path / ".config")
        data_home = _xdg_path(
            environment, "XDG_DATA_HOME", home_path / ".local" / "share"
        )
        cache_home = _xdg_path(environment, "XDG_CACHE_HOME", home_path / ".cache")
        state_home = _xdg_path(
            environment, "XDG_STATE_HOME", home_path / ".local" / "state"
        )
        runtime_fallback = _runtime_fallback_root()
        runtime_home = _xdg_runtime_path(
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


def default_downloads_dir(
    *,
    environ: Mapping[str, str] | None = None,
    home: str | os.PathLike[str] | None = None,
    platform_name: str | None = None,
) -> Path:
    """Return the user's Downloads directory with platform-safe fallbacks."""
    environment = os.environ if environ is None else environ
    platform_name = sys.platform if platform_name is None else platform_name
    home_path = _required_home_path(home)

    if platform_name.startswith("linux"):
        xdg_download_dir = _linux_xdg_user_dir(
            environment,
            "XDG_DOWNLOAD_DIR",
            home_path,
        )
        if xdg_download_dir is not None:
            return xdg_download_dir

    return home_path / "Downloads"


def _xdg_path(
    environment: Mapping[str, str], variable: str, fallback: Path
) -> Path:
    raw_value = environment.get(variable, "").strip()
    # The XDG specification requires absolute values; relative settings are
    # ignored rather than being resolved against an unpredictable cwd.
    if not raw_value or not os.path.isabs(raw_value):
        return fallback
    return Path(raw_value)


def _xdg_runtime_path(
    environment: Mapping[str, str], variable: str, fallback: Path
) -> Path:
    raw_value = environment.get(variable, "").strip()
    if not raw_value or not os.path.isabs(raw_value):
        return fallback
    candidate = Path(raw_value)
    if _is_valid_xdg_runtime_dir(candidate):
        return candidate
    return fallback


def _linux_xdg_user_dir(
    environment: Mapping[str, str],
    key: str,
    home_path: Path,
) -> Path | None:
    user_dirs_path = _xdg_path(
        environment,
        "XDG_CONFIG_HOME",
        home_path / ".config",
    ) / "user-dirs.dirs"
    try:
        lines = user_dirs_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None

    prefix = f"{key}="
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#") or not line.startswith(prefix):
            continue
        value = line[len(prefix):].strip()
        resolved = _resolve_xdg_user_dir_value(value, home_path)
        if resolved is not None:
            return resolved
    return None


def _resolve_xdg_user_dir_value(raw_value: str, home_path: Path) -> Path | None:
    if (
        len(raw_value) >= 2
        and raw_value[0] == raw_value[-1]
        and raw_value[0] in {"'", '"'}
    ):
        raw_value = raw_value[1:-1]
    if not raw_value:
        return None
    if raw_value == "$HOME":
        return home_path
    home_prefix = "$HOME/"
    if raw_value.startswith(home_prefix):
        return home_path / raw_value[len(home_prefix):]
    if os.path.isabs(raw_value):
        return Path(raw_value)
    return None


def _is_valid_xdg_runtime_dir(path: Path) -> bool:
    try:
        info = path.stat()
    except OSError:
        return False
    if not stat.S_ISDIR(info.st_mode):
        return False
    uid = _current_user_id()
    if uid is not None and getattr(info, "st_uid", uid) != uid:
        return False
    # XDG_RUNTIME_DIR is private per user.  The base directory must be 0700
    # rather than a loose shared directory.
    return stat.S_IMODE(info.st_mode) == 0o700


def _runtime_fallback_root() -> Path:
    suffix = _current_user_id()
    if suffix is None:
        suffix = os.getpid()
    return Path(tempfile.gettempdir()) / f"caveviewer-{suffix}"


def _required_absolute_path(variable: str, raw_value: str) -> Path:
    expanded = os.path.expanduser(raw_value)
    if not os.path.isabs(expanded):
        raise StoragePathError(f"{variable} must be an absolute path: {raw_value!r}")
    return Path(expanded)


def _required_home_path(home: str | os.PathLike[str] | None) -> Path:
    raw_value = os.fspath(home) if home is not None else os.path.expanduser("~")
    expanded = os.path.expanduser(raw_value)
    if not os.path.isabs(expanded):
        raise StoragePathError(
            f"home directory must resolve to an absolute path: {raw_value!r}"
        )
    return Path(expanded)


def _current_user_id() -> int | None:
    getuid = getattr(os, "getuid", None)
    if getuid is None:
        return None
    try:
        return int(getuid())
    except OSError:
        return None
