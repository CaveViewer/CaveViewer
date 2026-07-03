from __future__ import annotations

import logging
import os
import sys
from typing import Optional

_CONFIGURED = False


class _MaxLevelFilter(logging.Filter):
    def __init__(self, max_level: int):
        super().__init__()
        self.max_level = int(max_level)

    def filter(self, record: logging.LogRecord) -> bool:
        return record.levelno <= self.max_level


def _resolve_level(explicit_level: Optional[int | str] = None) -> int:
    if explicit_level is not None:
        if isinstance(explicit_level, int):
            return explicit_level
        return getattr(logging, str(explicit_level).upper(), logging.INFO)

    env_level = os.getenv("CAVEVIEWER_LOG_LEVEL", "").strip().upper()
    if env_level:
        return getattr(logging, env_level, logging.INFO)
    return logging.INFO


def configure_logging(level: Optional[int | str] = None, *, force: bool = False) -> None:
    """Configure CaveViewer logging once with a parse-friendly component prefix."""
    global _CONFIGURED
    if _CONFIGURED and not force:
        return

    log_level = _resolve_level(level)

    formatter = logging.Formatter(
        "[%(component)s] %(levelname)s: %(message)s",
        defaults={"component": "CaveViewer"},
    )

    stdout_handler = logging.StreamHandler(stream=sys.stdout)
    stdout_handler.setLevel(logging.DEBUG)
    stdout_handler.addFilter(_MaxLevelFilter(logging.WARNING))
    stdout_handler.setFormatter(formatter)

    stderr_handler = logging.StreamHandler(stream=sys.stderr)
    stderr_handler.setLevel(logging.ERROR)
    stderr_handler.setFormatter(formatter)

    root = logging.getLogger()
    root.setLevel(log_level)
    root.handlers.clear()
    root.addHandler(stdout_handler)
    root.addHandler(stderr_handler)

    _CONFIGURED = True


def get_logger(component: str) -> logging.LoggerAdapter:
    """Return a logger that emits with a stable [Component] prefix."""
    return logging.LoggerAdapter(logging.getLogger("caveviewer"), {"component": component})
