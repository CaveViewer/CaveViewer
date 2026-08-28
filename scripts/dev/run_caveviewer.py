"""Bootstrap runtime dependencies before launching CaveViewer from an IDE."""

from __future__ import annotations

import runpy
import subprocess
import sys
from pathlib import Path


def ensure_runtime_dependencies(project_root: Path) -> None:
    """Ask pip to install any missing or incompatible runtime requirements."""
    requirements = project_root / "requirements.txt"
    subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "--requirement",
            str(requirements),
        ],
        check=True,
    )


def main() -> None:
    """Prepare the selected interpreter, then run CaveViewer as a module."""
    project_root = Path(__file__).resolve().parents[2]
    ensure_runtime_dependencies(project_root)
    runpy.run_module("caveviewer", run_name="__main__", alter_sys=True)


if __name__ == "__main__":
    main()
