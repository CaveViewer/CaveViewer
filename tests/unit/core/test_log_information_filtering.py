"""Check real session and startup log files against the selected threshold."""

import logging

import pytest

from caveviewer.core.diagnostics.runtime import RuntimeDiagnostics
from caveviewer.core.diagnostics.startup import StartupDiagnostics


@pytest.mark.parametrize("diagnostics_type", [RuntimeDiagnostics, StartupDiagnostics])
@pytest.mark.parametrize("level", [logging.INFO, logging.DEBUG])
def test_file_handlers_enforce_information_level_even_for_debug_enabled_children(
    tmp_path, diagnostics_type, level,
):
    path = tmp_path / "session.log"
    root = logging.getLogger()
    child = logging.getLogger("caveviewer.test.log_information")
    previous_root_level, previous_child_level = root.level, child.level
    diagnostics = diagnostics_type(path)
    try:
        root.setLevel(level)
        child.setLevel(logging.DEBUG)
        diagnostics.attach_to_root_logger()
        child.debug("detail marker")
        child.info("information marker")
        child.warning("warning marker")
        child.error("error marker")
        child.critical("critical marker")
    finally:
        diagnostics.close()
        root.setLevel(previous_root_level)
        child.setLevel(previous_child_level)
    text = path.read_text(encoding="utf-8")
    assert ("detail marker" in text) == (level == logging.DEBUG)
    assert all(marker in text for marker in (
        "information marker", "warning marker", "error marker", "critical marker",
    ))
    assert diagnostics._handler not in root.handlers
