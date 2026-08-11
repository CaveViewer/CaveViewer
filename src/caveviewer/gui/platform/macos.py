"""macOS UI integration and verified-package reveal behavior."""

from __future__ import annotations

import os
import plistlib
import shutil
import subprocess

from caveviewer.core.diagnostics.logging import get_logger
from .base import DialogLayoutPolicy, PreferencesDialogLayoutPolicy, SplashLayoutPolicy
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

    def reveal_file(self, path: str) -> None:
        """Reveal a saved user file in Finder without opening the file."""
        subprocess.Popen(["open", "-R", os.path.abspath(path)])

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

    def bookmark_save_modifier(self) -> str:
        """On macOS, use Command key for bookmark saving."""
        return "command"

    def primary_shortcut_modifier_label(self) -> str:
        """Show macOS app shortcuts with the platform-native Command label."""
        return "Cmd"

    def mouse_look_button_name(self) -> str:
        """On macOS, use right-click for camera look (Option+left-click is also available)."""
        return "right"

    def compact_manual_controls_layout(self) -> bool:
        """Keep the macOS manual controls overlay at the roomier layout."""
        return False

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

    def splash_layout_policy(self) -> SplashLayoutPolicy:
        """Return macOS splash layout and Tk-root lifetime policy."""
        return SplashLayoutPolicy(
            app_icon_resource_name="app_icon_macos.png",
            reuse_existing_root=True,
            destroy_root_on_close=False,
            windows_layout=False,
            linux_layout=False,
            window_width=1100,
            min_height=680,
            extra_bottom_slack=36,
            secondary_link_row_bottom_gap=28,
            footer_credits_bottom_pad=24,
            title_to_action_gap=48,
            browse_button_bottom_gap=28,
            instruction_bottom_gap=16,
            secondary_link_row_top_gap=24,
        )

    def preferences_dialog_layout_policy(self) -> PreferencesDialogLayoutPolicy:
        """Return compact macOS Preferences layout values."""
        return PreferencesDialogLayoutPolicy(
            windows_layout=False,
            macos_layout=True,
            linux_layout=False,
            wrap_length=300,
            text_entry_width=24,
            body_pad_x=12,
            min_width=430,
            row_pad_x=14,
            row_pad_y=5,
            control_row_top_pad_y=5,
            tab_pad_x=10,
            tab_pad_y=6,
            tab_bottom_pad_y=8,
            button_row_top_pad_y=8,
            tab_highlight_thickness=0,
            notice_wrap_length=390,
        )

    def dialog_layout_policy(self) -> DialogLayoutPolicy:
        """Return macOS shared dialog layout values."""
        return DialogLayoutPolicy(body_pad_x=18, use_label_action_buttons=True)

    def tk_primary_modifier_name(self) -> str:
        """Return the Tk event modifier for macOS primary shortcuts."""
        return "Command"

    def default_text_antialiasing_mode(self) -> str:
        """Return the macOS default FreeType anti-aliasing mode."""
        return "light"

    def viewer_overlay_text_scale(self, base_scale: float) -> float:
        """Return the macOS OpenGL overlay text scale.

        CaveViewer draws the viewer HUD through FreeType instead of native
        AppKit controls, so it does not inherit macOS text-size preferences.
        A modest platform bump keeps the default closer to native Mac UI sizes
        while preserving explicit user/developer text-scale overrides.
        """
        return float(base_scale) * 1.15

    def tk_text_scale(self, default_font_points: float) -> float:
        """Return the macOS Tk text scale for splash and dialog fonts."""
        return max(super().tk_text_scale(default_font_points), 1.4)

    def suppress_forced_startup_focus(
        self, *, is_frozen: bool, force_requested: bool
    ) -> bool:
        """Suppress aggressive startup focusing for frozen macOS app bundles."""
        return bool(is_frozen and not force_requested)

    def command_modifier_uses_control_fallback(self) -> bool:
        """Some macOS backends report Command through Control-like flags."""
        return True

    def shift_digit_bookmark_save_fallback(self) -> bool:
        """Shift+digit remains a macOS backend fallback for bookmark saves."""
        return True

    def option_left_mouse_look_enabled(self) -> bool:
        """Allow Option+left-click and Option+motion as macOS mouse-look fallback."""
        return True

    def focus_viewer_window(self, window) -> None:
        """Use the least intrusive macOS activation path for viewer startup."""
        for target in (getattr(window, "_window", None), window):
            if target is None:
                continue
            try:
                if hasattr(target, "activate"):
                    target.activate()
                    break
            except Exception:
                pass
            try:
                if hasattr(target, "switch_to"):
                    target.switch_to()
                    break
            except Exception:
                pass
