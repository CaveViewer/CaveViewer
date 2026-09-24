#!/usr/bin/env python3
"""Shared helpers for the local CaveViewer website browser-test workflow."""

from __future__ import annotations

import os
import subprocess
import sys
import threading
from contextlib import contextmanager
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Iterator


WEBSITE_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = WEBSITE_ROOT.parent
BROWSER_TEST_ROOT = WEBSITE_ROOT / "tests" / "browser"
NODE_VERSION_FILE = BROWSER_TEST_ROOT / ".node-version"


def require_virtual_environment() -> Path:
    """Return the active virtual-environment prefix or raise a clear error."""

    if sys.prefix == sys.base_prefix:
        raise RuntimeError(
            "browser-test setup must run with the project virtual environment; "
            "use .venv\\Scripts\\python.exe on Windows or .venv/bin/python elsewhere"
        )
    return Path(sys.prefix).resolve()


def pinned_node_version() -> str:
    """Return the repository-pinned Node version."""

    version = NODE_VERSION_FILE.read_text(encoding="utf-8").strip()
    if not version:
        raise RuntimeError(f"Node version pin is empty: {NODE_VERSION_FILE}")
    return version


def environment_executable(
    environment: Path,
    name: str,
    *,
    platform_name: str | None = None,
) -> Path:
    """Resolve a Node-family executable directly from a Python environment."""

    platform_name = os.name if platform_name is None else platform_name
    if platform_name == "nt":
        suffix = ".exe" if name == "node" else ".cmd"
        executable = environment / "Scripts" / f"{name}{suffix}"
    else:
        executable = environment / "bin" / name
    if not executable.is_file():
        raise RuntimeError(
            f"{name} is not installed in {environment}. "
            "Run website/scripts/setup_browser_tests.py with the environment's Python."
        )
    return executable


def installed_node_version(node: Path) -> str | None:
    """Return the installed Node version without its leading ``v``."""

    try:
        completed = subprocess.run(
            [str(node), "--version"],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    return completed.stdout.strip().removeprefix("v")


def nodeenv_install_command(python: Path, version: str) -> list[str]:
    """Build the command that installs Node into the active Python environment."""

    return [
        str(python),
        "-m",
        "nodeenv",
        "-p",
        f"--node={version}",
        "--prebuilt",
    ]


def npm_ci_command(npm: Path) -> list[str]:
    """Build the locked browser dependency installation command."""

    return [str(npm), "ci", "--ignore-scripts"]


def playwright_install_command(npx: Path) -> list[str]:
    """Build the command that installs the pinned suite's Chromium browser."""

    return [str(npx), "playwright", "install", "chromium"]


class _QuietRequestHandler(SimpleHTTPRequestHandler):
    """Serve the staged website without printing per-request access logs."""

    def log_message(self, format: str, *args: object) -> None:
        return


@contextmanager
def managed_site_server(directory: Path) -> Iterator[str]:
    """Serve ``directory`` on one ephemeral loopback port and always stop it."""

    handler = partial(_QuietRequestHandler, directory=str(directory))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    server.daemon_threads = True
    thread = threading.Thread(
        target=server.serve_forever,
        name="caveviewer-website-tests",
        daemon=True,
    )
    thread.start()
    host, port = server.server_address[:2]
    try:
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        if thread.is_alive():
            raise RuntimeError("local website test server did not stop")
