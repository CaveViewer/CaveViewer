#!/usr/bin/env python3
"""Build and test the CaveViewer website with one managed local HTTP server."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

from browser_test_support import (
    BROWSER_TEST_ROOT,
    REPOSITORY_ROOT,
    environment_executable,
    installed_node_version,
    managed_site_server,
    pinned_node_version,
    require_virtual_environment,
)


def main() -> int:
    environment = require_virtual_environment()
    node = environment_executable(environment, "node")
    npm = environment_executable(environment, "npm")
    expected_version = pinned_node_version()
    actual_version = installed_node_version(node)
    if actual_version != expected_version:
        raise RuntimeError(
            f"expected Node {expected_version} in {environment}, found {actual_version}; "
            "rerun website/scripts/setup_browser_tests.py"
        )

    with tempfile.TemporaryDirectory(prefix="caveviewer-website-") as temp_dir:
        artifact = Path(temp_dir) / "site"
        subprocess.run(
            [
                sys.executable,
                str(REPOSITORY_ROOT / "website" / "scripts" / "build_site.py"),
                "--output",
                str(artifact),
            ],
            cwd=REPOSITORY_ROOT,
            check=True,
        )

        with managed_site_server(artifact) as base_url:
            environment_variables = os.environ.copy()
            environment_variables["CAVEVIEWER_WEBSITE_URL"] = base_url
            print(f"Running browser checks against {base_url}", flush=True)
            completed = subprocess.run(
                [str(npm), "test"],
                cwd=BROWSER_TEST_ROOT,
                env=environment_variables,
                check=False,
            )
            return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
