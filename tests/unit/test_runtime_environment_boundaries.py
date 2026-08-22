"""Guard the intentionally small process-environment boundary.

Normal application and benchmark composition resolve ``RuntimeSettings`` once
and inject typed values.  The modules below may still touch ``os.environ`` for
one of four explicit edge responsibilities: composing that snapshot, adapting
a child process, reading an operating-system/platform fact, or supporting a
documented standalone low-level caller.  Adding another module requires an
ownership decision here instead of silently reintroducing ambient settings.
"""

from __future__ import annotations

import ast
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
SOURCE_ROOT = REPO_ROOT / "src" / "caveviewer"

# Keep the reason beside the exception so reviews can distinguish intentional
# process boundaries from compatibility fallbacks that should be retired.
ALLOWED_ENVIRONMENT_MODULES = {
    "app.py": "application runtime-settings composition root",
    "benchmark.py": "benchmark runtime-settings composition root",
    "benchmarking/map_runner.py": "child benchmark process environment",
    "storage_paths.py": "injected storage/platform environment boundary",
    "core/chunking/builder.py": "standalone builder compatibility defaults",
    "core/chunking/capacity.py": "injected standalone capacity probe",
    "core/chunking/metadata.py": "standalone cache metadata compatibility",
    "core/chunking/upload.py": "injected standalone upload policy",
    "core/diagnostics/logging.py": "logging bootstrap before composition",
    "core/hardware/gpu_memory.py": "injected hardware probe boundary",
    "core/map/cache_paths.py": "injected standalone cache-path resolver",
    "core/mesh/obj.py": "standalone OBJ scanner compatibility",
    "core/preferences/schema.py": "pre-composition preference resolution",
    "core/streaming/world.py": "standalone streaming-world compatibility",
    "core/textures/decoding.py": "standalone texture decoder compatibility",
    "core/workers/priority.py": "injected worker-process policy boundary",
    "gui/bitmap_font.py": "standalone font renderer compatibility",
    "gui/dpi_utils.py": "pre-composition Tk scaling compatibility",
    "gui/import_process.py": "child import process serialization boundary",
    "gui/platform/portal.py": "desktop session facts",
    "gui/platform/process_priority.py": "injected native process policy",
    "gui/platform/probes/recording.py": "injected platform capability probe",
    "gui/platform/probes/updates.py": "injected packaging/platform probe",
    "gui/platform/probes/windowing.py": "injected display-server probe",
    "gui/platform/window_backend.py": "backend library process configuration",
    "gui/platform/windowing.py": "injected display-server policy",
    "gui/platform/windows_update_paths.py": "Windows LOCALAPPDATA platform fact",
    "gui/recording.py": "standalone recording compatibility",
    "gui/standard_library_maps.py": "standalone Map Library compatibility",
    "gui/viewer_window.py": "standalone viewer and benchmark compatibility",
}


def _imports_os(module: ast.Module) -> set[str]:
    aliases: set[str] = set()
    for node in module.body:
        if not isinstance(node, ast.Import):
            continue
        for imported in node.names:
            if imported.name == "os":
                aliases.add(imported.asname or "os")
    return aliases


def _uses_process_environment(path: Path) -> bool:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    os_aliases = _imports_os(tree)
    return any(
        isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id in os_aliases
        and node.attr in {"environ", "getenv", "putenv", "unsetenv"}
        for node in ast.walk(tree)
    )


def test_direct_process_environment_access_is_classified():
    actual = {
        path.relative_to(SOURCE_ROOT).as_posix()
        for path in SOURCE_ROOT.rglob("*.py")
        if _uses_process_environment(path)
    }

    assert actual == set(ALLOWED_ENVIRONMENT_MODULES), (
        "Direct process-environment access changed. Inject RuntimeSettings or "
        "an explicit mapping by default; if access is an intentional edge "
        "boundary, classify its ownership in ALLOWED_ENVIRONMENT_MODULES.\n"
        f"Added: {sorted(actual - set(ALLOWED_ENVIRONMENT_MODULES))}\n"
        f"Retired: {sorted(set(ALLOWED_ENVIRONMENT_MODULES) - actual)}"
    )


def test_every_environment_boundary_has_a_reason():
    assert all(reason.strip() for reason in ALLOWED_ENVIRONMENT_MODULES.values())
