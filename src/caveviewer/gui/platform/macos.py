"""macOS UI integration and saved-file reveal behavior."""

from __future__ import annotations

import os
import subprocess

from .default import DefaultSplashPlatformAdapter

class MacOSSplashPlatformAdapter(DefaultSplashPlatformAdapter):
    def reveal_file(self, path: str) -> None:
        """Reveal a saved user file in Finder without opening the file."""
        subprocess.Popen(["open", "-R", os.path.abspath(path)])
