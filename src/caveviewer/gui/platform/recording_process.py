"""Direct platform-owned configuration for recording encoder processes.

The adapter owns only non-command ``Popen`` options. Recording workflow code
continues to own ffmpeg commands, process lifetime, and worker threads.
"""

from __future__ import annotations

import subprocess
import sys
from typing import Any, Protocol


class RecordingProcessAdapter(Protocol):
    """Narrow boundary for platform-owned ffmpeg ``Popen`` configuration."""

    def encoder_popen_kwargs(self) -> dict[str, Any]:
        """Return non-command ``Popen`` options for a recording encoder."""


class DefaultRecordingProcessAdapter:
    """Launch encoders without platform-specific process flags."""

    def encoder_popen_kwargs(self) -> dict[str, Any]:
        return {}


class WindowsRecordingProcessAdapter:
    """Suppress console windows for GUI-launched recording encoders."""

    def encoder_popen_kwargs(self) -> dict[str, Any]:
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = 0
        return {
            "startupinfo": startupinfo,
            "creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0),
        }


def create_recording_process_adapter(
    *, platform_name: str | None = None
) -> RecordingProcessAdapter:
    """Compose direct encoder startup behavior from the platform name."""
    normalized_platform = str(platform_name or sys.platform).strip().lower()
    if normalized_platform.startswith("win"):
        return WindowsRecordingProcessAdapter()
    return DefaultRecordingProcessAdapter()
