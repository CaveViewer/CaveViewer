"""Developer-facing CLI for compiling CaveViewer map caches."""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from typing import Sequence

from caveviewer.core.logging_utils import configure_logging
from caveviewer.core.map_compiler import (
    CompileOptions,
    MapCompileConfigurationError,
    MapCompileError,
    compile_map,
)


PROGRAM_NAME = "caveviewer-chunker"

USAGE = f"""Usage:
  {PROGRAM_NAME} --source=<path> [options]

Compile a CaveViewer binary map cache without launching the viewer.

Required options:
  --source=<path>                    OBJ file, GLB file, or folder containing a map

Options:
  --cache-root=<path>                Managed cache root, same meaning as CAVEVIEWER_MAP_CACHE_DIR
  --settings-file=<path>             Advanced settings JSON to use
  --chunk-size=<value>               Import chunk size for new/rebuilt caches
  --obj-scan-throttle-ms=<value>     Milliseconds paused while scanning OBJ files
  --obj-import-batch-thousands=<n>   Thousands of triangulated OBJ faces per batch
  --obj-bucket-workers=<n>           Worker threads for temporary OBJ buckets
  --chunk-build-workers=<n>          Cache-building worker limit
  --chunk-build-reserved-cpus=<n>    Logical CPUs kept free during cache build
  --force                            Rebuild even if a valid matching cache already exists
  --dry-run                          Validate inputs and print the planned cache path
  --json                             Print machine-readable output
  -h, --help                         Show this help

Examples:
  {PROGRAM_NAME} --source=/maps/cave.obj --chunk-size=64
  {PROGRAM_NAME} --source=/maps/cave --cache-root=/data/caveviewer/maps --json

Defaults:
  Import options use built-in defaults unless overridden by CLI flags or an
  explicit --settings-file. Saved GUI Preferences are not loaded by default.

Built-in import defaults:
  --chunk-size=50
  --obj-scan-throttle-ms=0 on Linux/macOS, 1 on Windows
  --obj-import-batch-thousands=200
  --obj-bucket-workers=2
  --chunk-build-workers=1
  --chunk-build-reserved-cpus=2
"""


_VALUE_OPTIONS = {
    "--source": "source",
    "--cache-root": "cache_root",
    "--settings-file": "settings_file",
    "--chunk-size": "chunk_size_meters",
    "--obj-scan-throttle-ms": "obj_scan_throttle_ms",
    "--obj-import-batch-thousands": "obj_import_batch_thousands",
    "--obj-bucket-workers": "obj_bucket_workers",
    "--chunk-build-workers": "chunk_build_workers",
    "--chunk-build-reserved-cpus": "chunk_build_reserved_cpus",
}
_FLAG_OPTIONS = {
    "--force": "force_rebuild",
    "--dry-run": "dry_run",
    "--json": "json_output",
}
_HELP_OPTIONS = {"-h", "--help"}


class CliUsageError(ValueError):
    """Raised for command-line usage errors."""


@dataclass
class _ParsedCli:
    source: str | None = None
    cache_root: str | None = None
    settings_file: str | None = None
    parsing_overrides: dict[str, str] = field(default_factory=dict)
    obj_bucket_workers: str | None = None
    force_rebuild: bool = False
    dry_run: bool = False
    json_output: bool = False

    def to_compile_options(self) -> CompileOptions:
        if not self.source:
            raise CliUsageError("--source is required.")
        return CompileOptions(
            source=self.source,
            cache_root=self.cache_root,
            settings_file=self.settings_file,
            parsing_overrides=self.parsing_overrides,
            obj_bucket_workers=self.obj_bucket_workers,
            force_rebuild=self.force_rebuild,
            dry_run=self.dry_run,
            json_output=self.json_output,
        )


def parse_args(argv: Sequence[str]) -> _ParsedCli | None:
    """Parse CLI arguments using the strict project script conventions."""
    parsed = _ParsedCli()
    args = list(argv)
    index = 0
    while index < len(args):
        arg = args[index]
        if arg in _HELP_OPTIONS:
            return None
        if not arg.startswith("-"):
            raise CliUsageError(
                f"positional arguments are not supported: {arg!r}"
            )

        option, separator, inline_value = arg.partition("=")
        if option in _VALUE_OPTIONS:
            if separator:
                value = inline_value
            else:
                index += 1
                if index >= len(args):
                    raise CliUsageError(f"{option} requires a value.")
                value = args[index]
            if value == "":
                raise CliUsageError(f"{option} requires a value.")
            _store_value_option(parsed, option, value)
        elif option in _FLAG_OPTIONS:
            if separator:
                raise CliUsageError(f"{option} does not accept a value.")
            setattr(parsed, _FLAG_OPTIONS[option], True)
        else:
            raise CliUsageError(f"unknown option {option!r}")
        index += 1
    return parsed


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    try:
        parsed = parse_args(args)
        if parsed is None:
            print(USAGE, end="")
            return 0
        options = parsed.to_compile_options()
    except CliUsageError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2

    try:
        if not options.json_output:
            configure_logging()
        result = compile_map(options)
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        return 130
    except MapCompileConfigurationError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    except MapCompileError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    if options.json_output:
        print(json.dumps(result.as_dict(), sort_keys=True))
    else:
        _print_text_result(result)
    return 0


def _store_value_option(parsed: _ParsedCli, option: str, value: str) -> None:
    target = _VALUE_OPTIONS[option]
    if target in {"source", "cache_root", "settings_file"}:
        setattr(parsed, target, value)
    elif target == "obj_bucket_workers":
        parsed.obj_bucket_workers = value
    else:
        parsed.parsing_overrides[target] = value


def _print_text_result(result) -> None:
    if result.status == "planned":
        print(f"Planned cache directory: {result.cache_dir}")
    elif result.status == "skipped":
        print(f"Cache is up to date: {result.cache_dir}")
    else:
        print(f"Cache built: {result.cache_dir}")

    print(f"Source: {result.source_path}")
    print(f"Cache root: {result.cache_root}")
    print(f"Import chunk size: {result.chunk_size:g}")
    if result.chunk_count is not None:
        print(f"Chunks: {result.chunk_count}")
    if result.triangle_count is not None:
        print(f"Triangles: {result.triangle_count}")
    if result.rebuilt_for_chunk_size and result.status == "built":
        print("Rebuilt because the existing cache used a different chunk size.")


if __name__ == "__main__":
    raise SystemExit(main())
