"""Bootstrap runtime dependencies before launching CaveViewer from an IDE."""

from __future__ import annotations

import os
import runpy
import shutil
import subprocess
import sys
import sysconfig
from pathlib import Path


PYTHON_SERIES = "3.12"
UV_VERSION = "0.12.5"


def runtime_python(project_root: Path) -> Path:
    """Return the platform-specific interpreter in the managed environment."""
    if sys.platform == "win32":
        return project_root / ".venv" / "Scripts" / "python.exe"
    return project_root / ".venv" / "bin" / "python"


def interpreter_is_supported(interpreter: Path) -> bool:
    """Report whether an interpreter is usable and belongs to Python 3.12."""
    if not interpreter.is_file():
        return False
    completed = subprocess.run(
        [
            str(interpreter),
            "-c",
            "import sys; raise SystemExit(sys.version_info[:2] != (3, 12))",
        ],
        check=False,
    )
    return completed.returncode == 0


def find_uv() -> Path | None:
    """Find uv in standard locations for the bootstrap interpreter."""
    executable = "uv.exe" if sys.platform == "win32" else "uv"
    on_path = shutil.which(executable)
    if on_path:
        return Path(on_path)
    beside_interpreter = Path(sys.executable).resolve().parent / executable
    if beside_interpreter.is_file():
        return beside_interpreter

    # A non-admin pip install can place console scripts in the user scheme
    # without adding that directory to PATH, especially on Windows.
    user_scripts = Path(
        sysconfig.get_path("scripts", scheme=f"{os.name}_user")
    ) / executable
    return user_scripts if user_scripts.is_file() else None


def ensure_uv() -> Path:
    """Install the pinned runtime provisioner into the bootstrap interpreter."""
    existing = find_uv()
    if existing is not None:
        return existing
    subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            f"uv=={UV_VERSION}",
        ],
        check=True,
    )
    installed = find_uv()
    if installed is None:
        raise RuntimeError("uv was installed but its executable could not be found")
    return installed


def ensure_managed_runtime(project_root: Path) -> Path:
    """Create or reuse the repository-local managed Python 3.12 environment."""
    interpreter = runtime_python(project_root)
    if interpreter_is_supported(interpreter):
        return interpreter
    uv = ensure_uv()
    subprocess.run(
        [
            str(uv),
            "venv",
            "--python",
            PYTHON_SERIES,
            "--managed-python",
            "--seed",
            "--clear",
            str(project_root / ".venv"),
        ],
        check=True,
    )
    if not interpreter_is_supported(interpreter):
        raise RuntimeError("uv did not create a usable Python 3.12 environment")
    return interpreter


def ensure_managed_python(uv: Path) -> Path:
    """Install and locate Python 3.12 outside the active project environment."""
    subprocess.run([str(uv), "python", "install", PYTHON_SERIES], check=True)
    completed = subprocess.run(
        [str(uv), "python", "find", "--managed-python", PYTHON_SERIES],
        check=True,
        capture_output=True,
        text=True,
    )
    interpreter = Path(completed.stdout.strip())
    if not interpreter_is_supported(interpreter):
        raise RuntimeError("uv did not install a usable managed Python 3.12")
    return interpreter


def running_in(interpreter: Path) -> bool:
    """Report whether this process already uses the managed interpreter."""
    return Path(sys.executable).resolve() == interpreter.resolve()


def relaunch(interpreter: Path) -> None:
    """Restart the bootstrap without leaking the previous environment marker."""
    executable = str(interpreter)
    script = str(Path(__file__).resolve())
    environment = os.environ.copy()
    environment.pop("VIRTUAL_ENV", None)
    sys.stdout.flush()
    sys.stderr.flush()
    os.execve(executable, [executable, script, *sys.argv[1:]], environment)


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
    project_interpreter = runtime_python(project_root)
    if running_in(project_interpreter) and not interpreter_is_supported(
        project_interpreter
    ):
        relaunch(ensure_managed_python(ensure_uv()))
    interpreter = ensure_managed_runtime(project_root)
    if not running_in(interpreter):
        relaunch(interpreter)
    ensure_runtime_dependencies(project_root)
    runpy.run_module("caveviewer", run_name="__main__", alter_sys=True)


if __name__ == "__main__":
    main()
