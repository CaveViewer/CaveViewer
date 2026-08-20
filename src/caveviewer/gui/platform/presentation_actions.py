"""Focused native actions for Tk and viewer presentation.

The immutable :mod:`presentation` profile owns process-stable UI conventions.
This module owns only native side effects that must happen at action time:
process DPI setup, the macOS About handler, and best-effort viewer activation.
The selected adapter is constructed from the stable platform fact without
creating Tk objects, touching a display, or invoking native APIs.  Callers
invoke its methods from their existing Tk or viewer owning threads.
"""

from __future__ import annotations

import ctypes
import sys
from typing import Any, Protocol

from caveviewer.core.diagnostics.logging import get_logger


_LOG = get_logger("CaveViewer")

# Keep a strong reference to the Tk root used for the About handler so that
# Python's cyclic GC cannot collect it. The root must never be destroyed
# (splash callbacks use root.quit() instead of root.destroy()) because Tk
# registers a permanent NSApplicationDelegate for the process lifetime --
# macOS routes About-menu events through that delegate into this interpreter
# for as long as the app is running.
_macos_about_root_ref: Any | None = None


class PresentationActionsAdapter(Protocol):
    """Native presentation actions that do not belong in a static profile."""

    def configure_process_dpi_awareness(self) -> None:
        """Perform best-effort process-wide DPI setup before creating Tk."""

    def install_about_handler(
        self,
        root: Any,
        program_name: str,
        version: str,
    ) -> None:
        """Install the native About action for one already-created Tk root."""

    def focus_viewer_window(self, window: Any) -> None:
        """Best-effort activate an already-created viewer window."""


class DefaultPresentationActionsAdapter:
    """Conservative native presentation actions for generic desktops.

    This retains the historical default viewer-focus order: try the public
    window object and then its wrapped native object, calling ``switch_to``
    before ``activate`` on each target.  Individual action failures are
    best-effort no-ops, as they were on the broad adapter.
    """

    def configure_process_dpi_awareness(self) -> None:
        """Leave DPI setup to platforms that require a native action."""

    def install_about_handler(
        self,
        root: Any,
        program_name: str,
        version: str,
    ) -> None:
        """Leave non-macOS hosts without a native Tk About-menu action."""

    def focus_viewer_window(self, window: Any) -> None:
        """Best-effort foreground activation for generic window backends."""
        for target in (window, getattr(window, "_window", None)):
            if target is None:
                continue
            try:
                if hasattr(target, "switch_to"):
                    target.switch_to()
            except Exception:
                pass
            try:
                if hasattr(target, "activate"):
                    target.activate()
            except Exception:
                pass


class WindowsPresentationActionsAdapter(DefaultPresentationActionsAdapter):
    """Own the best-effort Windows process DPI configuration action."""

    def configure_process_dpi_awareness(self) -> None:
        """Configure Windows DPI awareness before creating Tk roots."""
        try:
            # Windows 10+: per-monitor DPI awareness v2.
            if ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
                return
        except Exception:
            pass

        try:
            # Windows 8.1+: per-monitor DPI awareness.
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
            return
        except Exception:
            pass

        try:
            # Vista fallback: system DPI awareness.
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


class MacOSPresentationActionsAdapter(DefaultPresentationActionsAdapter):
    """Own macOS Tk About registration and native viewer activation."""

    def install_about_handler(
        self,
        root: Any,
        program_name: str,
        version: str,
    ) -> None:
        """Install a Tcl-only About handler for one already-created Tk root."""
        global _macos_about_root_ref
        # Hold a module-level strong reference so the Tcl interpreter is
        # never freed by the GC (see module-level comment above).
        _macos_about_root_ref = root

        title = f"About {program_name}"
        message = f"{program_name}\nVersion {version}"
        detail = (
            "CaveViewer created by Brian Deatherage & Zsolt Zsabo of\n"
            "BottomLine Projects Scientific Dive Team and other volunteers.\n\n"
            "Licensed under the GNU General Public License v3.0."
        )

        # Register the About handler as PURE TCL PROCS rather than Python
        # callbacks. Python callbacks registered via root.createcommand()
        # go through _tkinter's PythonCmd() C function, which calls
        # PyEval_RestoreThread(tcl_tstate). tcl_tstate is a module-global
        # in _tkinter that is only non-NULL while an _tkinter call is
        # actively in progress (e.g. inside mainloop()). Once the splash
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
        except Exception as exc:
            _LOG.warning(f"could not install About handler: {exc}")

    def focus_viewer_window(self, window: Any) -> None:
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


def create_presentation_actions_adapter(
    *,
    platform_name: str | None = None,
) -> PresentationActionsAdapter:
    """Compose direct native presentation actions for one platform fact."""
    normalized_platform = str(platform_name or sys.platform).strip().lower()
    if normalized_platform == "darwin":
        return MacOSPresentationActionsAdapter()
    if normalized_platform.startswith("win"):
        return WindowsPresentationActionsAdapter()
    return DefaultPresentationActionsAdapter()
