from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time

from .base import ManualInstallResult
from .default import DefaultSplashPlatformAdapter


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
        about_state = {
            "open": False,
            "last_shown": 0.0,
        }

        def show_about_dialog(*_args):
            # Tk macOS command callbacks may pass arguments depending on Tk
            # version/menu plumbing. Also guard against late callbacks after
            # the splash root has already been destroyed.
            try:
                if not bool(root.winfo_exists()):
                    return ""
            except Exception:
                return ""

            now = time.monotonic()
            # Some Tk/macOS builds can trigger both callback names for one menu
            # action; debounce to prevent duplicate dialogs.
            if about_state["open"] or (now - about_state["last_shown"] < 0.75):
                return ""

            def _open():
                try:
                    about_state["open"] = True
                    about_state["last_shown"] = time.monotonic()
                    self._show_about_dialog(root, program_name, version)
                except Exception as e:
                    print(f"[CaveViewer] Failed to open About dialog: {e}")
                finally:
                    about_state["open"] = False
                    about_state["last_shown"] = time.monotonic()

            try:
                root.after_idle(_open)
            except Exception:
                _open()
            return ""

        try:
            root.createcommand("tkAboutDialog", show_about_dialog)
        except Exception:
            pass

        try:
            root.createcommand("::tk::mac::ShowAbout", show_about_dialog)
        except Exception:
            pass

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
                "BottomLine Projects Scientific Dive Team\n"
                "MacOS port by mr_v"
            ),
        )

    def default_update_repo(self) -> str:
        return "innerspace-explorer/CaveViewerMac"

    def update_check_user_agent(self) -> str:
        return "CaveViewerMac-UpdateChecker"

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
        """Return macOS-specific font file paths in priority order."""
        return [
            "/System/Library/Fonts/SFNS.ttf",
            "/System/Library/Fonts/SFNSDisplay.ttf",
            "/System/Library/Fonts/Supplemental/Helvetica.ttc",
            "/System/Library/Fonts/Supplemental/Arial.ttf",
            "/Library/Fonts/Arial.ttf",
            "/Library/Fonts/Helvetica.ttc",
        ]
