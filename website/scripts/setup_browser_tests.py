#!/usr/bin/env python3
"""Install the local website browser-test runtime into the active ``.venv``."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from browser_test_support import (
    BROWSER_TEST_ROOT,
    environment_executable,
    installed_node_version,
    nodeenv_install_command,
    npm_ci_command,
    pinned_node_version,
    playwright_install_command,
    require_virtual_environment,
)


def _run(command: list[str], *, cwd: Path | None = None) -> None:
    print(f"+ {subprocess.list2cmdline(command)}", flush=True)
    subprocess.run(command, cwd=cwd, check=True)


def main() -> int:
    environment = require_virtual_environment()
    expected_version = pinned_node_version()

    try:
        node = environment_executable(environment, "node")
        npm = environment_executable(environment, "npm")
        npx = environment_executable(environment, "npx")
    except RuntimeError:
        node = npm = npx = None

    if node is None or installed_node_version(node) != expected_version:
        _run(nodeenv_install_command(Path(sys.executable), expected_version))

    node = environment_executable(environment, "node")
    actual_version = installed_node_version(node)
    if actual_version != expected_version:
        raise RuntimeError(
            f"expected Node {expected_version} in {environment}, found {actual_version}"
        )

    npm = environment_executable(environment, "npm")
    npx = environment_executable(environment, "npx")
    _run(npm_ci_command(npm), cwd=BROWSER_TEST_ROOT)
    _run(playwright_install_command(npx), cwd=BROWSER_TEST_ROOT)

    print(f"Browser-test runtime is ready in {environment} (Node {actual_version}).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
