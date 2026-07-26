"""Opt-in JSONL diagnostics for Guided Dive troubleshooting."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
import json
import math
import os
import threading
import uuid
from typing import Any

import numpy as np


AUTO_DIVE_BLACKBOX_FILENAME = "auto_dive_debug.jsonl"
AUTO_DIVE_BLACKBOX_SCHEMA_VERSION = 2


class AutoDiveBlackbox:
    """Append-only structured Guided Dive diagnostic log.

    The writer is intentionally tiny and best-effort. Diagnostic failures must
    never interrupt viewer navigation, replanning, or shutdown.
    """

    def __init__(
        self,
        path: str | os.PathLike[str],
        *,
        session_id: str | None = None,
        clock: Callable[[], str] | None = None,
    ) -> None:
        self.path = os.path.abspath(os.fspath(path))
        self.session_id = session_id or uuid.uuid4().hex
        self._clock = clock or _utc_timestamp
        self._lock = threading.Lock()
        self._closed = False
        os.makedirs(os.path.dirname(self.path), exist_ok=True)

    def record(self, event: str, **payload: Any) -> None:
        """Append one JSON event, ignoring logging failures."""
        if self._closed:
            return
        record = {
            "ts": self._clock(),
            "session_id": self.session_id,
            "event": str(event),
            **payload,
            "schema_version": AUTO_DIVE_BLACKBOX_SCHEMA_VERSION,
        }
        line = json.dumps(_json_safe(record), sort_keys=True, separators=(",", ":"))
        try:
            with self._lock:
                if self._closed:
                    return
                with open(self.path, "a", encoding="utf-8") as file_obj:
                    file_obj.write(line)
                    file_obj.write("\n")
        except Exception:
            return

    def close(self) -> None:
        with self._lock:
            self._closed = True


def auto_dive_blackbox_path(cache_dir: str | os.PathLike[str]) -> str:
    """Return the blackbox JSONL path for a cache directory."""
    return os.path.join(os.path.abspath(os.fspath(cache_dir)), AUTO_DIVE_BLACKBOX_FILENAME)


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, np.generic):
        return _json_safe(value.item())
    if isinstance(value, np.ndarray):
        return _json_safe(value.tolist())
    if isinstance(value, Mapping):
        return {
            str(key): _json_safe(item)
            for key, item in value.items()
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_json_safe(item) for item in value]
    if isinstance(value, BaseException):
        return {
            "type": type(value).__name__,
            "message": str(value),
        }
    return str(value)
