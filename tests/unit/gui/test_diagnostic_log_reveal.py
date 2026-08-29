"""Tests for native diagnostic-log reveal adapters."""

from __future__ import annotations

from caveviewer.gui.platform import diagnostic_log_reveal


def test_windows_reveal_selects_log_in_explorer(tmp_path, monkeypatch):
    log_path = tmp_path / "viewer-session-test.log"
    log_path.write_text("test", encoding="utf-8")
    launched = []
    monkeypatch.setattr(
        diagnostic_log_reveal.subprocess,
        "Popen",
        lambda command: launched.append(command),
    )

    diagnostic_log_reveal.WindowsDiagnosticLogRevealAdapter().reveal_diagnostic_log(
        str(log_path)
    )

    assert launched == [["explorer", "/select,", str(log_path)]]


def test_macos_reveal_selects_log_in_finder(tmp_path, monkeypatch):
    log_path = tmp_path / "viewer-session-test.log"
    launched = []
    monkeypatch.setattr(
        diagnostic_log_reveal.subprocess,
        "Popen",
        lambda command: launched.append(command),
    )

    diagnostic_log_reveal.MacOSDiagnosticLogRevealAdapter().reveal_diagnostic_log(
        str(log_path)
    )

    assert launched == [["open", "-R", str(log_path.absolute())]]


def test_linux_reveal_uses_composed_desktop_service(tmp_path):
    log_path = tmp_path / "viewer-session-test.log"
    revealed = []

    class DesktopServices:
        def reveal_path(self, path, *, parent=None):
            revealed.append((path, parent))

    adapter = diagnostic_log_reveal.LinuxDiagnosticLogRevealAdapter(
        desktop_services=DesktopServices()
    )
    adapter.reveal_diagnostic_log(str(log_path))

    assert revealed == [(str(log_path), None)]


def test_unsupported_reveal_fails_explicitly():
    adapter = diagnostic_log_reveal.create_diagnostic_log_reveal_adapter(
        platform_name="freebsd"
    )

    try:
        adapter.reveal_diagnostic_log("/tmp/viewer-session-test.log")
    except RuntimeError as error:
        assert "unsupported" in str(error)
    else:
        raise AssertionError("unsupported adapter must fail explicitly")
