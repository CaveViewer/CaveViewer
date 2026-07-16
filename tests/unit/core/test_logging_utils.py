"""Tests for console logging/progress coordination."""

from __future__ import annotations

import io
import logging

from caveviewer.core import logging_utils


def test_logging_clears_and_redraws_active_progress(monkeypatch):
    stream = io.StringIO()
    root = logging.getLogger()
    old_handlers = list(root.handlers)
    old_level = root.level
    old_configured = logging_utils._CONFIGURED
    monkeypatch.setattr(logging_utils.sys, "stdout", stream)
    monkeypatch.setattr(logging_utils.sys, "stderr", stream)

    try:
        logging_utils.finish_console_progress_line()
        logging_utils.configure_logging(force=True)
        logging_utils.set_console_progress("bucketing faces", 0.43)

        logging_utils.get_logger("CaveViewer").info("Import heartbeat")
        logging_utils.finish_console_progress_line()
    finally:
        logging_utils.finish_console_progress_line()
        root.handlers.clear()
        root.handlers.extend(old_handlers)
        root.setLevel(old_level)
        logging_utils._CONFIGURED = old_configured

    output = stream.getvalue()
    progress = logging_utils.format_console_progress_line("bucketing faces", 0.43)
    assert output.startswith("\r" + progress)
    assert (
        "\r" + (" " * len(progress)) + "\r[caveviewer] INFO: Import heartbeat\n"
        in output
    )
    assert output.endswith("\r" + progress + "\n")


def test_configure_logging_routes_moderngl_window_through_caveviewer_format(
    monkeypatch,
):
    import moderngl_window

    stream = io.StringIO()
    root = logging.getLogger()
    old_root_handlers = list(root.handlers)
    old_root_level = root.level
    old_configured = logging_utils._CONFIGURED
    old_setup_basic_logging = moderngl_window.setup_basic_logging
    moderngl_logger = logging.getLogger("moderngl_window")
    old_moderngl_handlers = list(moderngl_logger.handlers)
    old_moderngl_propagate = moderngl_logger.propagate
    old_moderngl_level = moderngl_logger.level
    private_handler = logging.StreamHandler(stream)
    private_handler.setFormatter(logging.Formatter("PRIVATE %(message)s"))
    moderngl_logger.handlers[:] = [private_handler]
    moderngl_logger.propagate = False
    moderngl_logger.setLevel(logging.INFO)
    monkeypatch.setattr(logging_utils.sys, "stdout", stream)
    monkeypatch.setattr(logging_utils.sys, "stderr", stream)

    try:
        logging_utils.configure_logging(force=True)
        logging.getLogger("moderngl_window.context.base.window").info(
            "python: 3.14"
        )
        moderngl_window.setup_basic_logging(logging.INFO)
        logging.getLogger("moderngl_window.context.base.window").info(
            "platform: linux"
        )
    finally:
        logging_utils.finish_console_progress_line()
        root.handlers.clear()
        root.handlers.extend(old_root_handlers)
        root.setLevel(old_root_level)
        logging_utils._CONFIGURED = old_configured
        moderngl_logger.handlers[:] = old_moderngl_handlers
        moderngl_logger.propagate = old_moderngl_propagate
        moderngl_logger.setLevel(old_moderngl_level)
        moderngl_window.setup_basic_logging = old_setup_basic_logging

    output = stream.getvalue()
    assert "PRIVATE" not in output
    assert (
        "[moderngl_window.context.base.window] INFO: python: 3.14"
        in output
    )
    assert (
        "[moderngl_window.context.base.window] INFO: platform: linux"
        in output
    )


def test_configure_logging_normalizes_explicit_component(monkeypatch):
    stream = io.StringIO()
    root = logging.getLogger()
    old_handlers = list(root.handlers)
    old_level = root.level
    old_configured = logging_utils._CONFIGURED
    monkeypatch.setattr(logging_utils.sys, "stdout", stream)
    monkeypatch.setattr(logging_utils.sys, "stderr", stream)

    try:
        logging_utils.configure_logging(force=True)
        logging.getLogger("caveviewer").info(
            "child import started",
            extra={"component": "ImportProcess"},
        )
        logging_utils.get_logger("StreamingWorld").info("worker ready")
    finally:
        logging_utils.finish_console_progress_line()
        root.handlers.clear()
        root.handlers.extend(old_handlers)
        root.setLevel(old_level)
        logging_utils._CONFIGURED = old_configured

    output = stream.getvalue()
    assert "[import_process] INFO: child import started" in output
    assert "[streaming_world] INFO: worker ready" in output


def test_component_normalization_uses_stable_parseable_ids():
    assert logging_utils._normalize_component_name("CaveViewer") == "caveviewer"
    assert logging_utils._normalize_component_name("ImportProcess") == "import_process"
    assert logging_utils._normalize_component_name("StreamingWorld") == "streaming_world"
    assert logging_utils._normalize_component_name("TextureManager") == "texture_manager"
    assert (
        logging_utils._normalize_component_name(
            "moderngl_window.context.base.window"
        )
        == "moderngl_window.context.base.window"
    )


def test_progress_line_shortening_clears_previous_content(monkeypatch):
    stream = io.StringIO()
    monkeypatch.setattr(logging_utils.sys, "stdout", stream)

    logging_utils.finish_console_progress_line()
    logging_utils.set_console_progress_line("long progress message")
    logging_utils.set_console_progress_line("short")
    logging_utils.finish_console_progress_line()

    expected_padding = len("long progress message") - len("short")
    assert "\rshort" + (" " * expected_padding) in stream.getvalue()
