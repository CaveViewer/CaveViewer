"""Developer-facing CLI for compiling CaveViewer map caches."""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from typing import Sequence

from caveviewer.core.logging_utils import (
    configure_logging,
    finish_console_progress_line,
    set_console_progress,
)
from caveviewer.core.map_compiler import (
    CompileOptions,
    MapCompileConfigurationError,
    MapCompileError,
    analyze_chunk_sizes,
    compile_map,
)


PROGRAM_NAME = "caveviewer-chunker"

USAGE = f"""Usage:
  {PROGRAM_NAME} --source=<path> [options]

Compile a CaveViewer binary map cache without launching the viewer.

Required options:
  --source=<path>                    OBJ file, GLB file, or folder containing a map

Options:
  --cache-root=<path>                Root folder where compiled map caches are stored
  --settings-file=<path>             Advanced settings JSON to use
  --chunk-size=<value>               Import chunk size for new/rebuilt caches
  --max-upload-group-mb=<value>      Target maximum VBO upload group size in MB
  --obj-scan-throttle-ms=<value>     Milliseconds paused while scanning OBJ files
  --obj-import-batch-thousands=<n>   Thousands of triangulated OBJ faces per batch
  --obj-bucket-workers=<n>           Worker threads for temporary OBJ buckets
  --chunk-build-workers=<n>          Cache-building worker limit
  --chunk-build-reserved-cpus=<n>    Logical CPUs kept free during cache build
  --analyze-chunk-sizes              Analyze source geometry and recommend a chunk size
  --analyze-workers=<n>              Worker threads for chunk-size analysis
  --force                            Rebuild even if a valid matching cache already exists
  --dry-run                          Validate inputs and print the planned cache path
  --json                             Print machine-readable output
  -h, --help                         Show this help

Examples:
  {PROGRAM_NAME} --source=/maps/cave.obj --chunk-size=64
  {PROGRAM_NAME} --source=/maps/cave.obj --analyze-chunk-sizes
  {PROGRAM_NAME} --source=/maps/cave --cache-root=/data/caveviewer/maps --json

Source checkout:
  .venv-dev/bin/python -m caveviewer.chunker --source=/maps/cave.obj --chunk-size=64

Defaults:
  Import options use built-in defaults unless overridden by CLI flags or an
  explicit --settings-file. Saved GUI Preferences are not loaded by default.

Built-in import defaults:
  --chunk-size=50
  --max-upload-group-mb=32
  --obj-scan-throttle-ms=0 on Linux/macOS, 1 on Windows
  --obj-import-batch-thousands=200
  --obj-bucket-workers=2
  --chunk-build-workers=1
  --chunk-build-reserved-cpus=2

Analysis defaults:
  --analyze-workers=2

Cache root default:
  --cache-root defaults to the same managed map-cache root used by the GUI:
    Linux:   $XDG_CACHE_HOME/caveviewer/maps, or ~/.cache/caveviewer/maps
    macOS:   ~/.caveviewer/maps
    Windows: %USERPROFILE%\\.caveviewer\\maps
"""


_VALUE_OPTIONS = {
    "--source": "source",
    "--cache-root": "cache_root",
    "--settings-file": "settings_file",
    "--chunk-size": "chunk_size_meters",
    "--max-upload-group-mb": "max_upload_group_mb",
    "--obj-scan-throttle-ms": "obj_scan_throttle_ms",
    "--obj-import-batch-thousands": "obj_import_batch_thousands",
    "--obj-bucket-workers": "obj_bucket_workers",
    "--chunk-build-workers": "chunk_build_workers",
    "--chunk-build-reserved-cpus": "chunk_build_reserved_cpus",
    "--analyze-workers": "analyze_workers",
}
_FLAG_OPTIONS = {
    "--analyze-chunk-sizes": "analyze_chunk_sizes",
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
    analyze_workers: str | None = None
    analyze_chunk_sizes: bool = False
    force_rebuild: bool = False
    dry_run: bool = False
    json_output: bool = False

    def to_compile_options(self) -> CompileOptions:
        if not self.source:
            raise CliUsageError("--source is required.")
        if self.analyze_workers is not None and not self.analyze_chunk_sizes:
            raise CliUsageError(
                "--analyze-workers requires --analyze-chunk-sizes."
            )
        return CompileOptions(
            source=self.source,
            cache_root=self.cache_root,
            settings_file=self.settings_file,
            parsing_overrides=self.parsing_overrides,
            obj_bucket_workers=self.obj_bucket_workers,
            analyze_workers=self.analyze_workers,
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
        result = (
            _analyze_with_optional_progress(options)
            if parsed.analyze_chunk_sizes
            else compile_map(options)
        )
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
    elif parsed.analyze_chunk_sizes:
        _print_text_analysis(result)
    else:
        _print_text_result(result)
    return 0


def _store_value_option(parsed: _ParsedCli, option: str, value: str) -> None:
    target = _VALUE_OPTIONS[option]
    if target in {"source", "cache_root", "settings_file"}:
        setattr(parsed, target, value)
    elif target == "obj_bucket_workers":
        parsed.obj_bucket_workers = value
    elif target == "analyze_workers":
        parsed.analyze_workers = value
    else:
        parsed.parsing_overrides[target] = value


def _analyze_with_optional_progress(options: CompileOptions):
    if options.json_output:
        return analyze_chunk_sizes(options)
    try:
        return analyze_chunk_sizes(options, progress_cb=set_console_progress)
    finally:
        finish_console_progress_line()


def _print_text_result(result) -> None:
    if result.status == "planned":
        print("Planned cache:")
    elif result.status == "skipped":
        print("Cache is up to date:")
    else:
        print("Cache built:")

    _print_field("Cache", result.cache_dir)
    _print_field("Source", result.source_path)
    _print_field("Cache root", result.cache_root)
    _print_field("Import chunk size", f"{result.chunk_size:g}")
    if result.chunk_count is not None:
        _print_field("Chunks", _format_count(result.chunk_count))
    if result.triangle_count is not None:
        _print_field("Triangles", _format_count(result.triangle_count))
    if result.elapsed_seconds is not None:
        _print_field("Elapsed", _format_seconds(result.elapsed_seconds))
    if result.rebuilt_for_chunk_size and result.status == "built":
        _print_field("Reason", "existing cache used a different chunk size")


def _print_field(label: str, value: object) -> None:
    print(f"  {label}: {value}")


def _format_count(value: int) -> str:
    return f"{int(value):,}"


def _format_seconds(value: float) -> str:
    return f"{float(value):.1f}s"


def _print_text_analysis(recommendation) -> None:
    print(f"Recommended chunk size: {recommendation.recommended_size:g}")
    print(f"Reason: {recommendation.explanation}")
    print("Candidate scores:")
    for candidate in recommendation.candidates:
        warning_suffix = ""
        if candidate.warnings:
            warning_suffix = f" warnings={'; '.join(candidate.warnings)}"
        print(
            f"  {candidate.chunk_size:>6g}  "
            f"score={candidate.score:.4f}  "
            f"chunks={_format_count(candidate.chunk_count)}  "
            f"p95_faces={_format_count(candidate.p95_chunk_faces)}  "
            f"max_faces={_format_count(candidate.max_chunk_faces)}  "
            f"p95_est={_format_bytes(candidate.p95_chunk_bytes_estimate)}"
            f"{warning_suffix}"
        )


def _format_bytes(value: int) -> str:
    size = float(value)
    units = ("B", "KiB", "MiB", "GiB")
    unit = units[0]
    for unit in units:
        if size < 1024.0 or unit == units[-1]:
            break
        size /= 1024.0
    if unit == "B":
        return f"{int(size)} {unit}"
    return f"{size:.1f} {unit}"


if __name__ == "__main__":
    raise SystemExit(main())
