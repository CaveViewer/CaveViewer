"""Cover the developer-facing caveviewer-chunker command interface."""

from __future__ import annotations

import json

from caveviewer import chunker as chunker_cli
from caveviewer.core.map_compiler import CompileResult


def _result(status: str = "planned") -> CompileResult:
    return CompileResult(
        status=status,
        source_argument="/maps/cave.glb",
        source_path="/maps/cave.glb",
        source_format="glb",
        textures_dir="/maps",
        cache_root="/cache/maps",
        cache_dir="/cache/maps/cave-123",
        chunk_size=64.0,
        chunk_count=3,
        triangle_count=9,
    )


def test_help_uses_public_command_name(capsys):
    assert chunker_cli.main(["-h"]) == 0

    output = capsys.readouterr().out
    assert output.startswith("Usage:\n  caveviewer-chunker --source=<path>")
    assert "--profile" not in output
    assert "Saved GUI Preferences are not loaded by default." in output
    assert "Built-in import defaults:" in output
    assert "--chunk-size=50" in output
    assert "--obj-bucket-workers=2" in output
    assert "Env-only:" not in output


def test_cli_rejects_positional_arguments(capsys):
    assert chunker_cli.main(["/maps/cave"]) == 2

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Error: positional arguments are not supported: '/maps/cave'" in captured.err


def test_cli_rejects_unknown_options(capsys):
    assert chunker_cli.main(["--source=/maps/cave", "--bad"]) == 2

    captured = capsys.readouterr()
    assert "Error: unknown option '--bad'" in captured.err


def test_cli_rejects_missing_option_values(capsys):
    assert chunker_cli.main(["--source"]) == 2

    captured = capsys.readouterr()
    assert "Error: --source requires a value." in captured.err


def test_cli_passes_named_options_to_compiler(monkeypatch, capsys):
    received = []

    def fake_compile(options):
        received.append(options)
        return _result(status="built")

    monkeypatch.setattr(chunker_cli, "compile_map", fake_compile)
    monkeypatch.setattr(chunker_cli, "configure_logging", lambda: None)

    exit_code = chunker_cli.main(
        [
            "--source",
            "/maps/cave.glb",
            "--cache-root=/cache/maps",
            "--chunk-size=64",
            "--obj-scan-throttle-ms=2",
            "--obj-import-batch-thousands=250",
            "--obj-bucket-workers=4",
            "--chunk-build-workers=4",
            "--chunk-build-reserved-cpus=2",
            "--force",
        ]
    )

    assert exit_code == 0
    options = received[0]
    assert options.source == "/maps/cave.glb"
    assert options.cache_root == "/cache/maps"
    assert options.force_rebuild is True
    assert options.obj_bucket_workers == "4"
    assert options.parsing_overrides == {
        "chunk_size_meters": "64",
        "obj_scan_throttle_ms": "2",
        "obj_import_batch_thousands": "250",
        "chunk_build_workers": "4",
        "chunk_build_reserved_cpus": "2",
    }
    assert "Cache built: /cache/maps/cave-123" in capsys.readouterr().out


def test_json_output_is_machine_readable(monkeypatch, capsys):
    monkeypatch.setattr(chunker_cli, "compile_map", lambda _options: _result())

    assert chunker_cli.main(["--source=/maps/cave.glb", "--json"]) == 0

    captured = capsys.readouterr()
    assert captured.err == ""
    payload = json.loads(captured.out)
    assert payload["status"] == "planned"
    assert payload["cache_dir"] == "/cache/maps/cave-123"
    assert payload["chunk_size"] == 64.0
