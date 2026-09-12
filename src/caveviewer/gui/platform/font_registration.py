"""Process-private registration lifecycle for bundled desktop UI fonts."""

from __future__ import annotations

import ctypes
import ctypes.util
import sys
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from caveviewer.core.diagnostics.logging import get_logger
from caveviewer.resources import inter_font_paths


_LOG = get_logger("CaveViewer")
_WINDOWS_PRIVATE_FONT = 0x10
_MACOS_PROCESS_SCOPE = 1


class PrivateFontApi(Protocol):
    """Small native boundary used by the process registration lifecycle."""

    def register(self, path: Path) -> bool:
        """Register one font file for the current process."""

    def unregister(self, path: Path) -> bool:
        """Release one font file from the current process."""


@dataclass(frozen=True, slots=True)
class FontRegistrationResult:
    """Stable outcome of one process font-registration attempt."""

    success: bool
    registered_paths: tuple[Path, ...]
    diagnostic: str | None = None


class ProcessFontRegistration:
    """Own an idempotent group of process-private native font registrations."""

    def __init__(self, *, api: PrivateFontApi, font_paths: Sequence[Path]) -> None:
        self._api = api
        self._font_paths = tuple(Path(path) for path in font_paths)
        self._result: FontRegistrationResult | None = None
        self._closed = False

    @property
    def result(self) -> FontRegistrationResult | None:
        return self._result

    def activate(self) -> FontRegistrationResult:
        """Register every face once, rolling back a partial native registration."""
        if self._result is not None:
            return self._result
        if self._closed:
            self._result = FontRegistrationResult(
                success=False,
                registered_paths=(),
                diagnostic="font registration is already closed",
            )
            return self._result

        missing = tuple(path for path in self._font_paths if not path.is_file())
        if missing:
            self._result = FontRegistrationResult(
                success=False,
                registered_paths=(),
                diagnostic="one or more bundled Inter files are missing",
            )
            return self._result

        registered: list[Path] = []
        try:
            for path in self._font_paths:
                if not self._api.register(path):
                    raise RuntimeError(f"native registration rejected {path.name}")
                registered.append(path)
        except Exception as exc:
            for path in reversed(registered):
                try:
                    self._api.unregister(path)
                except Exception:
                    _LOG.debug(
                        "Bundled-font rollback failed for %s",
                        path.name,
                        exc_info=True,
                    )
            self._result = FontRegistrationResult(
                success=False,
                registered_paths=(),
                diagnostic=str(exc),
            )
            return self._result

        self._result = FontRegistrationResult(
            success=True,
            registered_paths=tuple(registered),
        )
        return self._result

    def close(self) -> None:
        """Release successfully registered faces once."""
        if self._closed:
            return
        self._closed = True
        result = self._result
        if result is None or not result.success:
            return
        for path in reversed(result.registered_paths):
            try:
                self._api.unregister(path)
            except Exception:
                _LOG.debug(
                    "Bundled-font cleanup failed for %s",
                    path.name,
                    exc_info=True,
                )


def _configure_ctypes_function(function, *, argtypes, restype) -> None:
    """Apply ctypes signatures while allowing plain injected test callables."""
    try:
        function.argtypes = argtypes
        function.restype = restype
    except (AttributeError, TypeError):
        pass


class WindowsPrivateFontApi:
    """Register fonts with GDI's process-private `FR_PRIVATE` scope."""

    def __init__(
        self,
        *,
        add_font: Callable[..., int] | None = None,
        remove_font: Callable[..., int] | None = None,
    ) -> None:
        if add_font is None or remove_font is None:
            gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
            add_font = gdi32.AddFontResourceExW
            remove_font = gdi32.RemoveFontResourceExW
        _configure_ctypes_function(
            add_font,
            argtypes=(ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_void_p),
            restype=ctypes.c_int,
        )
        _configure_ctypes_function(
            remove_font,
            argtypes=(ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_void_p),
            restype=ctypes.c_int,
        )
        self._add_font = add_font
        self._remove_font = remove_font

    def register(self, path: Path) -> bool:
        return bool(self._add_font(str(path), _WINDOWS_PRIVATE_FONT, None))

    def unregister(self, path: Path) -> bool:
        return bool(self._remove_font(str(path), _WINDOWS_PRIVATE_FONT, None))


class MacOSProcessFontApi:
    """Register file URLs with CoreText's process scope."""

    def __init__(
        self,
        *,
        create_url: Callable[..., int] | None = None,
        register_fonts: Callable[..., bool] | None = None,
        unregister_fonts: Callable[..., bool] | None = None,
        release: Callable[[object], None] | None = None,
    ) -> None:
        if any(
            function is None
            for function in (create_url, register_fonts, unregister_fonts, release)
        ):
            core_foundation = ctypes.CDLL(
                "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
            )
            core_text = ctypes.CDLL(
                "/System/Library/Frameworks/CoreText.framework/CoreText"
            )
            create_url = core_foundation.CFURLCreateFromFileSystemRepresentation
            register_fonts = core_text.CTFontManagerRegisterFontsForURL
            unregister_fonts = core_text.CTFontManagerUnregisterFontsForURL
            release = core_foundation.CFRelease
        _configure_ctypes_function(
            create_url,
            argtypes=(ctypes.c_void_p, ctypes.c_char_p, ctypes.c_long, ctypes.c_ubyte),
            restype=ctypes.c_void_p,
        )
        for function in (register_fonts, unregister_fonts):
            _configure_ctypes_function(
                function,
                argtypes=(ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(ctypes.c_void_p)),
                restype=ctypes.c_bool,
            )
        _configure_ctypes_function(
            release,
            argtypes=(ctypes.c_void_p,),
            restype=None,
        )
        self._create_url = create_url
        self._register_fonts = register_fonts
        self._unregister_fonts = unregister_fonts
        self._release = release

    def _font_url(self, path: Path):
        encoded = str(path).encode("utf-8")
        return self._create_url(None, encoded, len(encoded), False)

    def _call_manager(self, function: Callable[..., bool], path: Path) -> bool:
        url = self._font_url(path)
        if not url:
            return False
        error = ctypes.c_void_p()
        try:
            return bool(function(url, _MACOS_PROCESS_SCOPE, ctypes.byref(error)))
        finally:
            if error.value:
                self._release(error.value)
            self._release(url)

    def register(self, path: Path) -> bool:
        return self._call_manager(self._register_fonts, path)

    def unregister(self, path: Path) -> bool:
        return self._call_manager(self._unregister_fonts, path)


class LinuxApplicationFontApi:
    """Register application fonts with the process's current Fontconfig config."""

    def __init__(
        self,
        *,
        config: int | None = None,
        add_font: Callable[..., int] | None = None,
        clear_fonts: Callable[..., None] | None = None,
        build_fonts: Callable[..., int] | None = None,
    ) -> None:
        if add_font is None or clear_fonts is None or build_fonts is None:
            library_name = ctypes.util.find_library("fontconfig")
            if not library_name:
                raise RuntimeError("Fontconfig is unavailable")
            fontconfig = ctypes.CDLL(library_name)
            get_current = fontconfig.FcConfigGetCurrent
            _configure_ctypes_function(get_current, argtypes=(), restype=ctypes.c_void_p)
            config = get_current()
            add_font = fontconfig.FcConfigAppFontAddFile
            clear_fonts = fontconfig.FcConfigAppFontClear
            build_fonts = fontconfig.FcConfigBuildFonts
        if not config:
            raise RuntimeError("Fontconfig has no current configuration")
        _configure_ctypes_function(
            add_font,
            argtypes=(ctypes.c_void_p, ctypes.c_char_p),
            restype=ctypes.c_int,
        )
        _configure_ctypes_function(
            clear_fonts,
            argtypes=(ctypes.c_void_p,),
            restype=None,
        )
        _configure_ctypes_function(
            build_fonts,
            argtypes=(ctypes.c_void_p,),
            restype=ctypes.c_int,
        )
        self._config = config
        self._add_font = add_font
        self._clear_fonts = clear_fonts
        self._build_fonts = build_fonts

    def register(self, path: Path) -> bool:
        added = bool(self._add_font(self._config, str(path).encode("utf-8")))
        if not added:
            return False
        if bool(self._build_fonts(self._config)):
            return True
        self._clear_fonts(self._config)
        self._build_fonts(self._config)
        return False

    def unregister(self, path: Path) -> bool:
        del path
        self._clear_fonts(self._config)
        return bool(self._build_fonts(self._config))


def create_private_font_api(*, platform_name: str | None = None) -> PrivateFontApi:
    """Create the native private-font adapter for one normalized platform."""
    normalized = str(platform_name or sys.platform).strip().lower()
    if normalized.startswith("win"):
        return WindowsPrivateFontApi()
    if normalized == "darwin":
        return MacOSProcessFontApi()
    if normalized.startswith("linux"):
        return LinuxApplicationFontApi()
    raise RuntimeError(f"private font registration is unsupported on {normalized!r}")


_REGISTRATION_LOCK = threading.RLock()
_PROCESS_REGISTRATION: ProcessFontRegistration | None = None


def ensure_bundled_inter_registered(
    *,
    platform_name: str | None = None,
    font_paths: Sequence[Path] | None = None,
    api: PrivateFontApi | None = None,
) -> FontRegistrationResult:
    """Register bundled Inter faces once and return a nonfatal stable result."""
    global _PROCESS_REGISTRATION
    with _REGISTRATION_LOCK:
        if _PROCESS_REGISTRATION is not None:
            existing = _PROCESS_REGISTRATION.result
            if existing is not None:
                return existing
        try:
            resolved_api = api or create_private_font_api(platform_name=platform_name)
            registration = ProcessFontRegistration(
                api=resolved_api,
                font_paths=(
                    inter_font_paths() if font_paths is None else font_paths
                ),
            )
            result = registration.activate()
        except Exception as exc:
            registration = ProcessFontRegistration(
                api=api or _UnavailableFontApi(),
                font_paths=(),
            )
            result = FontRegistrationResult(False, (), str(exc))
            registration._result = result
        _PROCESS_REGISTRATION = registration
        if result.success:
            _LOG.debug("Registered %d bundled Inter faces", len(result.registered_paths))
        else:
            _LOG.warning("Bundled Inter registration unavailable: %s", result.diagnostic)
        return result


class _UnavailableFontApi:
    """No-op owner retained after native adapter construction fails."""

    def register(self, path: Path) -> bool:
        del path
        return False

    def unregister(self, path: Path) -> bool:
        del path
        return False


def unregister_bundled_inter_fonts() -> None:
    """Release and forget the process-owned Inter registration."""
    global _PROCESS_REGISTRATION
    with _REGISTRATION_LOCK:
        registration = _PROCESS_REGISTRATION
        _PROCESS_REGISTRATION = None
    if registration is not None:
        registration.close()
