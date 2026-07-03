#!/usr/bin/env python3
"""Detached updater used by the splash screen inline update flow.

Usage:
    python updater.py <mode> <payload_path> <expected_sha256>

Modes:
    macos_app: verify payload and open it for manual installation.
"""

import hashlib
import os
import sys
import tempfile
import time

try:
    from core.logging_utils import configure_logging, get_logger
    from gui.platform import get_platform_adapter
except ModuleNotFoundError:
    # Supports running as a plain script via `python gui/updater.py`.
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if repo_root not in sys.path:
        sys.path.insert(0, repo_root)
    from core.logging_utils import configure_logging, get_logger
    from gui.platform import get_platform_adapter

_PLATFORM_ADAPTER = get_platform_adapter()
_LOG = get_logger("Updater")
_TEMP_CLEANUP_PREFIXES = (
    "caveviewer_update_mount_",
    "caveviewer_update_stage_",
    "caveviewer_update_",
    "caveviewer_updater_",
)


def _truncate_log(log_path):
    try:
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        with open(log_path, "w"):
            pass
    except Exception:
        pass


def log(log_path, message):
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{timestamp}] {message}"
    _LOG.info(message)
    try:
        with open(log_path, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


def _safe_remove_path(path):
    try:
        if not path:
            return
        if os.path.isdir(path):
            import shutil
            shutil.rmtree(path, ignore_errors=True)
        elif os.path.exists(path):
            os.remove(path)
    except Exception:
        pass


def _cleanup_temp_artifacts(log_path=None, extra_paths=None, preserve_paths=None):
    temp_root = tempfile.gettempdir()
    preserve_abs = set()
    if preserve_paths:
        for path in preserve_paths:
            if path:
                preserve_abs.add(os.path.abspath(path))
    try:
        for name in os.listdir(temp_root):
            candidate = os.path.join(temp_root, name)
            candidate_abs = os.path.abspath(candidate)
            if candidate_abs in preserve_abs:
                continue
            if any(name.startswith(prefix) for prefix in _TEMP_CLEANUP_PREFIXES):
                _safe_remove_path(candidate)
    except Exception as e:
        if log_path:
            log(log_path, f"WARNING: temp cleanup scan failed: {e}")

    if extra_paths:
        for path in extra_paths:
            if path and os.path.abspath(path) not in preserve_abs:
                _safe_remove_path(path)


def _verify_payload_sha256(payload_path, expected_sha256, log_path):
    expected = (expected_sha256 or "").strip().lower()
    if not expected:
        raise RuntimeError("Missing expected SHA-256 for update payload.")

    sha256 = hashlib.sha256()
    with open(payload_path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            sha256.update(chunk)
    actual = sha256.hexdigest().lower()

    if actual != expected:
        raise RuntimeError(
            "Payload SHA-256 mismatch. Expected "
            f"{expected}, got {actual}."
        )

    log(log_path, "Payload SHA-256 verification passed.")

def main():
    configure_logging()
    if len(sys.argv) < 4:
        _LOG.error("Usage: python updater.py <mode> <payload_path> <expected_sha256>")
        sys.exit(1)

    mode = (sys.argv[1] or _PLATFORM_ADAPTER.install_channel()).strip().lower()
    payload_path = sys.argv[2]
    expected_sha256 = sys.argv[3] if len(sys.argv) > 3 else None

    log_path = os.path.join(tempfile.gettempdir(), "caveviewer_update_log.txt")
    _truncate_log(log_path)

    updater_temp_dir = None
    try:
        updater_temp_dir = os.path.dirname(os.path.abspath(__file__))
    except Exception:
        updater_temp_dir = None

    _cleanup_temp_artifacts(log_path=log_path, preserve_paths=[log_path, payload_path])
    log(log_path, f"Updater started. mode={mode}, payload={payload_path}")

    supported_modes = _PLATFORM_ADAPTER.updater_supported_modes()
    if mode not in supported_modes:
        log(log_path, f"ERROR: unsupported update mode '{mode}'. Supported modes: {sorted(supported_modes)}")
        sys.exit(1)

    time.sleep(1.0)

    if not os.path.exists(payload_path):
        log(log_path, f"ERROR: downloaded payload not found at {payload_path}. Aborting update.")
        sys.exit(1)

    try:
        _verify_payload_sha256(payload_path, expected_sha256, log_path)

        _PLATFORM_ADAPTER.launch_payload_for_mode(mode, payload_path, lambda message: log(log_path, message))
        log(log_path, "Manual install flow started. Install from the downloaded package and relaunch CaveViewer manually.")
        log(log_path, "Updater finished.")
    except Exception as e:
        log(log_path, f"ERROR during update flow: {e}")
        sys.exit(1)
    finally:
        _cleanup_temp_artifacts(
            log_path=log_path,
            extra_paths=[updater_temp_dir] if updater_temp_dir and os.path.basename(updater_temp_dir).startswith("caveviewer_updater_") else None,
            preserve_paths=[log_path, payload_path],
        )


if __name__ == "__main__":
    main()
