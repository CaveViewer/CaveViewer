"""Process-private bundled-font registration and cleanup contracts."""

from __future__ import annotations

from pathlib import Path

from caveviewer.gui.platform.font_registration import (
    LinuxApplicationFontApi,
    MacOSProcessFontApi,
    ProcessFontRegistration,
    WindowsPrivateFontApi,
    ensure_bundled_inter_registered,
    unregister_bundled_inter_fonts,
)


class _RecordingApi:
    def __init__(self, *, rejected_name: str | None = None) -> None:
        self.rejected_name = rejected_name
        self.registered: list[str] = []
        self.unregistered: list[str] = []

    def register(self, path: Path) -> bool:
        self.registered.append(path.name)
        return path.name != self.rejected_name

    def unregister(self, path: Path) -> bool:
        self.unregistered.append(path.name)
        return True


def _font_files(tmp_path: Path) -> tuple[Path, ...]:
    paths = tuple(tmp_path / name for name in ("regular.ttf", "semibold.ttf", "bold.ttf"))
    for path in paths:
        path.write_bytes(b"font")
    return paths


def test_process_registration_is_idempotent_and_closes_in_reverse(tmp_path: Path) -> None:
    api = _RecordingApi()
    paths = _font_files(tmp_path)
    registration = ProcessFontRegistration(api=api, font_paths=paths)

    first = registration.activate()
    second = registration.activate()
    registration.close()
    registration.close()

    assert first is second
    assert first.success is True
    assert first.registered_paths == paths
    assert api.registered == [path.name for path in paths]
    assert api.unregistered == [path.name for path in reversed(paths)]


def test_process_registration_rolls_back_partial_failure(tmp_path: Path) -> None:
    api = _RecordingApi(rejected_name="semibold.ttf")
    paths = _font_files(tmp_path)

    result = ProcessFontRegistration(api=api, font_paths=paths).activate()

    assert result.success is False
    assert result.registered_paths == ()
    assert result.diagnostic == "native registration rejected semibold.ttf"
    assert api.registered == ["regular.ttf", "semibold.ttf"]
    assert api.unregistered == ["regular.ttf"]


def test_process_registration_rejects_missing_files_before_native_calls(
    tmp_path: Path,
) -> None:
    api = _RecordingApi()

    result = ProcessFontRegistration(
        api=api,
        font_paths=(tmp_path / "missing.ttf",),
    ).activate()

    assert result.success is False
    assert result.diagnostic == "one or more bundled Inter files are missing"
    assert api.registered == []


def test_process_singleton_reuses_result_and_cleanup_allows_fresh_registration(
    tmp_path: Path,
) -> None:
    unregister_bundled_inter_fonts()
    first_api = _RecordingApi()
    second_api = _RecordingApi()
    paths = _font_files(tmp_path)
    try:
        first = ensure_bundled_inter_registered(font_paths=paths, api=first_api)
        repeated = ensure_bundled_inter_registered(font_paths=paths, api=second_api)
        unregister_bundled_inter_fonts()
        fresh = ensure_bundled_inter_registered(font_paths=paths, api=second_api)
    finally:
        unregister_bundled_inter_fonts()

    assert first is repeated
    assert first.success is True
    assert fresh.success is True
    assert first_api.registered == [path.name for path in paths]
    assert first_api.unregistered == [path.name for path in reversed(paths)]
    assert second_api.registered == [path.name for path in paths]


def test_windows_api_uses_private_scope_for_add_and_remove(tmp_path: Path) -> None:
    calls: list[tuple[str, str, int, object]] = []

    def add(path: str, flags: int, reserved: object) -> int:
        calls.append(("add", path, flags, reserved))
        return 1

    def remove(path: str, flags: int, reserved: object) -> int:
        calls.append(("remove", path, flags, reserved))
        return 1

    path = tmp_path / "Inter Regular.ttf"
    api = WindowsPrivateFontApi(add_font=add, remove_font=remove)

    assert api.register(path) is True
    assert api.unregister(path) is True
    assert calls == [
        ("add", str(path), 0x10, None),
        ("remove", str(path), 0x10, None),
    ]


def test_macos_api_uses_utf8_file_url_and_process_scope(tmp_path: Path) -> None:
    calls: list[tuple] = []

    def create_url(allocator, encoded: bytes, length: int, is_directory: bool) -> int:
        calls.append(("url", allocator, encoded, length, is_directory))
        return 41

    def register(url: int, scope: int, error) -> bool:
        calls.append(("register", url, scope, error))
        return True

    def unregister(url: int, scope: int, error) -> bool:
        calls.append(("unregister", url, scope, error))
        return True

    def release(reference: int) -> None:
        calls.append(("release", reference))

    path = tmp_path / "Inter-Regular.ttf"
    encoded = str(path).encode("utf-8")
    api = MacOSProcessFontApi(
        create_url=create_url,
        register_fonts=register,
        unregister_fonts=unregister,
        release=release,
    )

    assert api.register(path) is True
    assert api.unregister(path) is True
    assert calls[0] == ("url", None, encoded, len(encoded), False)
    assert calls[1][0:3] == ("register", 41, 1)
    assert calls[2] == ("release", 41)
    assert calls[4][0:3] == ("unregister", 41, 1)
    assert calls[5] == ("release", 41)


def test_linux_api_adds_utf8_file_and_rebuilds_then_clears(tmp_path: Path) -> None:
    calls: list[tuple] = []

    def add(config: int, encoded: bytes) -> int:
        calls.append(("add", config, encoded))
        return 1

    def clear(config: int) -> None:
        calls.append(("clear", config))

    def build(config: int) -> int:
        calls.append(("build", config))
        return 1

    path = tmp_path / "Inter-Regular.ttf"
    api = LinuxApplicationFontApi(
        config=73,
        add_font=add,
        clear_fonts=clear,
        build_fonts=build,
    )

    assert api.register(path) is True
    assert api.unregister(path) is True
    assert calls == [
        ("add", 73, str(path).encode("utf-8")),
        ("build", 73),
        ("clear", 73),
        ("build", 73),
    ]
