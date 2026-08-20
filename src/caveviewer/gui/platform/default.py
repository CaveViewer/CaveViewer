"""Default GUI platform behavior for unsupported or generic desktops."""

from __future__ import annotations

from .base import SplashPlatformAdapter


class DefaultSplashPlatformAdapter(SplashPlatformAdapter):
    """Conservative action defaults for unsupported or generic desktops."""

    def reveal_file(self, path: str) -> None:
        raise RuntimeError(
            f"Revealing files is unsupported on this platform: {path}"
        )

    def load_system_certificates(self, context) -> None:
        """Load any platform certificate stores needed by urllib SSL contexts."""
        return None

    def recording_subprocess_startup_kwargs(self) -> dict:
        """Return subprocess kwargs for GUI-launched recording encoders."""
        return {}
