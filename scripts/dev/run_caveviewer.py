"""Bootstrap runtime dependencies before launching CaveViewer from an IDE."""

from __future__ import annotations

import runpy
import subprocess
import sys
from pathlib import Path


def require_supported_python() -> None:
    """Reject interpreters outside CaveViewer's supported Python series."""
    if sys.version_info[:2] != (3, 12):
        selected = ".".join(str(part) for part in sys.version_info[:3])
        raise RuntimeError(
            "CaveViewer requires Python 3.12, but the selected interpreter "
            f"is Python {selected}: {sys.executable}"
        )


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
    require_supported_python()
    ensure_runtime_dependencies(project_root)
    runpy.run_module("caveviewer", run_name="__main__", alter_sys=True)


if __name__ == "__main__":
    main()
