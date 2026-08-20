"""Windows GUI platform adapter implementation."""

from __future__ import annotations

import ssl
import subprocess

from .default import DefaultSplashPlatformAdapter
from .windows_explorer import explorer_select_command


class WindowsSplashPlatformAdapter(DefaultSplashPlatformAdapter):
    """Windows GUI integration and saved-file reveal behavior."""

    def reveal_file(self, path: str) -> None:
        """Reveal a saved user file in Explorer without opening the file."""
        subprocess.Popen(explorer_select_command(path))

    def load_system_certificates(self, context) -> None:
        """Trust Windows certificate stores in addition to Python's bundle."""
        for store_name in ("CA", "ROOT"):
            try:
                for cert, enc, _trust in ssl.enum_certificates(store_name):
                    if enc == "x509_asn":
                        try:
                            context.load_verify_locations(cadata=cert)
                        except ssl.SSLError:
                            pass
            except (AttributeError, OSError):
                pass

    def recording_subprocess_startup_kwargs(self) -> dict:
        """Hide console windows for GUI-launched ffmpeg recording."""
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = 0
        return {
            "startupinfo": startupinfo,
            "creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0),
        }
