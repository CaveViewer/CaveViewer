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
    assert "\r" + (" " * len(progress)) + "\r[CaveViewer] INFO: Import heartbeat\n" in output
    assert output.endswith("\r" + progress + "\n")


def test_progress_line_shortening_clears_previous_content(monkeypatch):
    stream = io.StringIO()
    monkeypatch.setattr(logging_utils.sys, "stdout", stream)

    logging_utils.finish_console_progress_line()
    logging_utils.set_console_progress_line("long progress message")
    logging_utils.set_console_progress_line("short")
    logging_utils.finish_console_progress_line()

    expected_padding = len("long progress message") - len("short")
    assert "\rshort" + (" " * expected_padding) in stream.getvalue()
