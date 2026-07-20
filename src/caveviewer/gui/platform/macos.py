"""macOS UI integration and architecture-specific update behavior."""

from __future__ import annotations

import os
import platform
import plistlib
import shutil
import subprocess

from caveviewer.core.diagnostics.logging import get_logger
from .default import DefaultSplashPlatformAdapter

# Keep a strong reference to the Tk root used for the About handler so that
# Python's cyclic GC cannot collect it.  The root must never be destroyed
# (splash callbacks use root.quit() instead of root.destroy()) because Tk
# registers a permanent NSApplicationDelegate for the process lifetime --
# macOS routes About-menu events through that delegate into this interpreter
# for as long as the app is running.
_about_root_ref = None
_LOG = get_logger("CaveViewer")


class MacOSSplashPlatformAdapter(DefaultSplashPlatformAdapter):
    def __init__(self):
        # Reuse an existing mount for repeated "Show in Finder" actions rather
        # than attaching the same DMG once per click.
        self._mounted_payloads: dict[str, tuple[str, str]] = {}

    def ui_font_family(self) -> str:
        return "Helvetica Neue"

    def install_channel(self) -> str:
        return "macos_app"

    def persist_downloaded_payload(self, temp_payload_path: str, download_url: str | None) -> str:
        downloads_dir = os.path.join(os.path.expanduser("~"), "Downloads")
        os.makedirs(downloads_dir, exist_ok=True)

        url_basename = ""
        if download_url:
            url_basename = os.path.basename(download_url.split("?", 1)[0]).strip()
        if not url_basename.lower().endswith(".dmg"):
            url_basename = "CaveViewer-latest.dmg"

        final_path = os.path.join(downloads_dir, url_basename)
        if os.path.exists(final_path):
            base, ext = os.path.splitext(final_path)
            suffix = 1
            candidate = f"{base}-{suffix}{ext}"
            while os.path.exists(candidate):
                suffix += 1
                candidate = f"{base}-{suffix}{ext}"
            final_path = candidate

        shutil.move(temp_payload_path, final_path)
        return final_path

    def download_reveal_action_label(self) -> str:
        return "Show in Finder"

    def reveal_downloaded_payload(self, payload_path: str) -> None:
        payload_path = os.path.abspath(payload_path)
        if not payload_path.lower().endswith(".dmg"):
            subprocess.Popen(["open", "-R", payload_path])
            return

        cached = self._mounted_payloads.get(payload_path)
        if cached is not None:
            mountpoint, reveal_path = cached
            if os.path.exists(mountpoint) and os.path.exists(reveal_path):
                self._reveal_in_finder(mountpoint, reveal_path)
                return
            self._mounted_payloads.pop(payload_path, None)

        completed = subprocess.run(
            [
                "hdiutil",
                "attach",
                payload_path,
                "-nobrowse",
                "-readonly",
                "-plist",
            ],
            check=True,
            capture_output=True,
        )
        attach_result = plistlib.loads(completed.stdout)
        mountpoint = next(
            (
                entity.get("mount-point")
                for entity in attach_result.get("system-entities", ())
                if entity.get("mount-point")
            ),
            None,
        )
        if not mountpoint:
            raise RuntimeError(f"Mounted DMG did not report a mount point: {payload_path}")

        app_path = None
        for root_dir, dir_names, _ in os.walk(mountpoint):
            for dir_name in dir_names:
                if dir_name.endswith(".app"):
                    app_path = os.path.join(root_dir, dir_name)
                    break
            if app_path:
                break

        reveal_path = app_path or mountpoint
        self._mounted_payloads[payload_path] = (mountpoint, reveal_path)
        self._reveal_in_finder(mountpoint, reveal_path)

    @staticmethod
    def _reveal_in_finder(mountpoint: str, reveal_path: str) -> None:
        if reveal_path != mountpoint:
            subprocess.Popen(["open", "-R", reveal_path])
        else:
            subprocess.Popen(["open", mountpoint])

    def install_about_handler(self, root, program_name: str, version: str) -> None:
        global _about_root_ref
        # Hold a module-level strong reference so the Tcl interpreter is
        # never freed by the GC (see module-level comment above).
        _about_root_ref = root

        title = f"About {program_name}"
        message = f"{program_name}\nVersion {version}"
        detail = (
            "CaveViewer created by Brian Deatherage & Zsolt Zsabo of\n"
            "BottomLine Projects Scientific Dive Team and other volunteers.\n\n"
            "Licensed under the GNU General Public License v3.0."
        )

        # Register the About handler as PURE TCL PROCS rather than Python
        # callbacks.  Python callbacks registered via root.createcommand()
        # go through _tkinter's PythonCmd() C function, which calls
        # PyEval_RestoreThread(tcl_tstate).  tcl_tstate is a module-global
        # in _tkinter that is only non-NULL while an _tkinter call is
        # actively in progress (e.g. inside mainloop()).  Once the splash
        # screen closes and the OpenGL viewer window takes over, no
        # _tkinter call is active, so tcl_tstate is NULL -- and the next
        # About-menu click triggers PythonCmd -> PyEval_RestoreThread(NULL)
        # -> _Py_FatalError -> SIGABRT crash.
        #
        # Pure Tcl procs bypass PythonCmd entirely: Tcl executes them
        # directly in the Tcl interpreter without touching Python's GIL
        # machinery, so the crash cannot occur regardless of whether the
        # Tk mainloop is running.
        #
        # Use root.call() to set the string variables so Python newlines
        # are passed as Tcl objects directly (no manual Tcl escaping).
        try:
            root.call("set", "_cv_about_title", title)
            root.call("set", "_cv_about_msg", message)
            root.call("set", "_cv_about_detail", detail)
            root.eval(
                "proc ::tk::mac::ShowAbout {} {\n"
                "    global _cv_about_title _cv_about_msg _cv_about_detail\n"
                "    catch {tk_messageBox -type ok"
                " -title $_cv_about_title"
                " -message $_cv_about_msg"
                " -detail $_cv_about_detail} _cv_about_err\n"
                "}\n"
                "proc tkAboutDialog {} { ::tk::mac::ShowAbout }"
            )
        except Exception as e:
            _LOG.warning(f"could not install About handler: {e}")

    def _show_about_dialog(self, parent, program_name: str, version: str) -> None:
        # Use the native tk_messageBox path directly and avoid setting an icon.
        # Putting credits in "detail" keeps the secondary text smaller and helps
        # the dialog fit better on small displays.
        parent.tk.call(
            "tk_messageBox",
            "-parent", str(parent),
            "-type", "ok",
            "-title", f"About {program_name}",
            "-message", f"{program_name}\nVersion {version}",
            "-detail", (
                "CaveViewer created by Brian Deatherage & Zsolt Zsabo of\n"
                "BottomLine Projects Scientific Dive Team and other volunteers.\n\n"
                "Licensed under the GNU General Public License v3.0."
            ),
        )

    def default_update_repo(self) -> str:
        return "CaveViewer/CaveViewer"

    def default_update_manifest_url(self, repo: str, branch: str) -> str:
        architecture = _macos_process_architecture()
        if architecture is None:
            # An explicit 404 is safer than offering an incompatible binary.
            architecture = "unsupported"
        return (
            f"https://raw.githubusercontent.com/{repo}/{branch}/"
            f"updates/macos/{architecture}/stable.json"
        )

    def update_check_user_agent(self) -> str:
        return "CaveViewer-UpdateChecker"

    def supports_install_channel(self, channel: str) -> bool:
        return channel == "macos_app"

    def unsupported_install_channel_message(self, channel: str) -> str:
        return f"Unsupported install channel '{channel}'. macOS updates are DMG-only."

    def channel_download_url_keys(self, channel: str) -> tuple[str, ...]:
        if channel == "macos_app":
            return ("download_url_macosx_dmg", "download_url_macos", "download_url")
        return super().channel_download_url_keys(channel)

    def channel_download_size_keys(self, channel: str) -> tuple[str, ...]:
        if channel == "macos_app":
            return ("download_size_bytes_macosx_dmg", "download_size_bytes_macos", "download_size_bytes")
        return super().channel_download_size_keys(channel)

    def channel_sha256_keys(self, channel: str) -> tuple[str, ...]:
        if channel == "macos_app":
            return ("sha256_macosx_dmg", "sha256_macos", "sha256")
        return super().channel_sha256_keys(channel)

    def missing_download_url_message(self, channel: str) -> str:
        if channel == "macos_app":
            return "Update manifest is missing required field: download_url_macosx_dmg."
        return super().missing_download_url_message(channel)

    def bookmark_save_modifier(self) -> str:
        """On macOS, use Command key for bookmark saving."""
        return "command"

    def mouse_look_button_name(self) -> str:
        """On macOS, use right-click for camera look (Option+left-click is also available)."""
        return "right"

    def font_candidates(self) -> list[str]:
        """Return macOS-specific font file paths in priority order.

        SFNS.ttf (San Francisco) is the primary macOS UI font and renders
        well in FreeType's light mode (the macOS default set in bitmap_font).
        HelveticaNeue.ttc is a robust fallback -- it carries proper TrueType
        hinting and renders cleanly across all sizes even in normal mode.
        """
        return [
            "/System/Library/Fonts/SFNS.ttf",
            "/System/Library/Fonts/SFNSDisplay.ttf",
            "/System/Library/Fonts/HelveticaNeue.ttc",
            "/System/Library/Fonts/Helvetica.ttc",
            "/System/Library/Fonts/Supplemental/Helvetica.ttc",
            "/System/Library/Fonts/Supplemental/Arial.ttf",
            "/Library/Fonts/Arial.ttf",
        ]


def _macos_process_architecture(machine: str | None = None) -> str | None:
    """Return the canonical architecture of this macOS process.

    Process architecture is intentional: a CaveViewer process running through
    Rosetta should receive the x86_64 build, even on Apple Silicon hardware.
    """
    detected = (machine if machine is not None else platform.machine()).strip().lower()
    if detected in {"arm64", "aarch64"}:
        return "arm64"
    if detected in {"x86_64", "amd64", "x64"}:
        return "x86_64"
    return None
