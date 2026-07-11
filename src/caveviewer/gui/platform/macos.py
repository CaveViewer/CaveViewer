from __future__ import annotations

import os
import shutil
import subprocess
import tempfile

from caveviewer.core.logging_utils import get_logger
from .base import ManualInstallResult
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

    def prepare_manual_install(self, payload_path: str) -> ManualInstallResult:
        mountpoint = tempfile.mkdtemp(prefix="caveviewer_manual_update_mount_")
        subprocess.run(
            ["hdiutil", "attach", payload_path, "-nobrowse", "-readonly", "-mountpoint", mountpoint],
            check=True,
            capture_output=True,
            text=True,
        )

        app_path = None
        for root_dir, dir_names, _ in os.walk(mountpoint):
            for dir_name in dir_names:
                if dir_name.endswith(".app"):
                    app_path = os.path.join(root_dir, dir_name)
                    break
            if app_path:
                break

        if app_path and os.path.exists(app_path):
            subprocess.Popen(["open", "-R", app_path])
        else:
            subprocess.Popen(["open", mountpoint])

        return ManualInstallResult(mounted_payload_path=mountpoint, mounted_app_path=app_path)

    def bookmark_save_modifier(self) -> str:
        """On macOS, use Command key for bookmark saving."""
        return "command"

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
        return "KernalPanic/CaveViewer"

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

    def updater_supported_modes(self) -> set[str]:
        return {"macos_app"}

    def launch_payload_for_mode(self, mode: str, payload_path: str, log_func) -> None:
        if mode != "macos_app":
            return super().launch_payload_for_mode(mode, payload_path, log_func)
        try:
            subprocess.Popen(["open", payload_path])
            log_func(f"Opened in Finder for manual install: {payload_path}")
        except Exception as e:
            raise RuntimeError(f"Could not open path in Finder ({payload_path}): {e}")

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
