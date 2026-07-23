"""Cover the developer-facing caveviewer-chunker command interface."""

from __future__ import annotations

import json

from caveviewer import chunker as chunker_cli
from caveviewer.core.map.chunk_size_advisor import (
    ChunkSizeCandidate,
    ChunkSizeRecommendation,
)
from caveviewer.core.map.compiler import CompileResult


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
        chunk_count=4_765,
        triangle_count=162_341_299,
        elapsed_seconds=2668.2 if status == "built" else None,
    )


def _recommendation() -> ChunkSizeRecommendation:
    return ChunkSizeRecommendation(
        recommended_size=64.0,
        explanation="Selected 64 because it had the lowest score.",
        candidates=(
            ChunkSizeCandidate(
                chunk_size=50.0,
                score=0.42,
                chunk_count=10,
                median_chunk_faces=100,
                p95_chunk_faces=500,
                max_chunk_faces=700,
                median_chunk_bytes_estimate=9600,
                p95_chunk_bytes_estimate=48000,
                max_chunk_bytes_estimate=67200,
                median_material_count=1,
                p95_material_count=2,
                occupancy_sparsity=0.1,
                direction_change_score=0.2,
            ),
            ChunkSizeCandidate(
                chunk_size=64.0,
                score=0.25,
                chunk_count=7,
                median_chunk_faces=160,
                p95_chunk_faces=620,
                max_chunk_faces=900,
                median_chunk_bytes_estimate=15360,
                p95_chunk_bytes_estimate=59520,
                max_chunk_bytes_estimate=86400,
                median_material_count=1,
                p95_material_count=2,
                occupancy_sparsity=0.2,
                direction_change_score=0.1,
            ),
        ),
    )


def test_help_uses_public_command_name(capsys):
    assert chunker_cli.main(["-h"]) == 0

    output = capsys.readouterr().out
    assert output.startswith("Usage:\n  caveviewer-chunker --source=<path>")
    assert "--profile" not in output
    assert "Saved GUI Preferences are not loaded by default." in output
    assert "Built-in import defaults:" in output
    assert ".venv-dev/bin/python -m caveviewer.chunker" in output
    assert "--analyze-chunk-sizes" in output
    assert "--analyze-workers=<n>" in output
    assert "--analyze-workers=2" in output
    assert "--chunk-size=50" in output
    assert "--obj-bucket-workers=2" in output
    assert "Cache root default:" in output
    assert "$XDG_CACHE_HOME/caveviewer/maps" in output
    assert "%USERPROFILE%\\.caveviewer\\maps" in output
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
            "--settings-file=/settings/chunker.json",
            "--chunk-size=64",
            "--max-upload-group-mb=24",
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
    assert options.settings_file == "/settings/chunker.json"
    assert options.force_rebuild is True
    assert options.obj_bucket_workers == "4"
    assert options.parsing_overrides == {
        "chunk_size_meters": "64",
        "max_upload_group_mb": "24",
        "obj_scan_throttle_ms": "2",
        "obj_import_batch_thousands": "250",
        "chunk_build_workers": "4",
        "chunk_build_reserved_cpus": "2",
    }
    output = capsys.readouterr().out
    assert "Cache built:\n" in output
    assert "  Cache: /cache/maps/cave-123\n" in output
    assert "  Chunks: 4,765\n" in output
    assert "  Triangles: 162,341,299\n" in output
    assert "  Elapsed: 2668.2s\n" in output


def test_json_output_is_machine_readable(monkeypatch, capsys):
    monkeypatch.setattr(chunker_cli, "compile_map", lambda _options: _result())

    assert chunker_cli.main(["--source=/maps/cave.glb", "--json"]) == 0

    captured = capsys.readouterr()
    assert captured.err == ""
    payload = json.loads(captured.out)
    assert payload["status"] == "planned"
    assert payload["cache_dir"] == "/cache/maps/cave-123"
    assert payload["chunk_size"] == 64.0
    assert payload["elapsed_seconds"] is None


def test_analyze_chunk_sizes_bypasses_compile(monkeypatch, capsys):
    received = []
    progress = []
    finished = []

    def fake_analyze(options, *, progress_cb=None):
        received.append(options)
        if progress_cb is not None:
            progress_cb("reading source", 0.25)
        return _recommendation()

    monkeypatch.setattr(chunker_cli, "analyze_chunk_sizes", fake_analyze)
    monkeypatch.setattr(
        chunker_cli,
        "set_console_progress",
        lambda stage, fraction: progress.append((stage, fraction)),
    )
    monkeypatch.setattr(
        chunker_cli,
        "finish_console_progress_line",
        lambda: finished.append(True) or True,
    )
    monkeypatch.setattr(
        chunker_cli,
        "compile_map",
        lambda _options: raise_assertion("analysis must not compile"),
    )
    monkeypatch.setattr(chunker_cli, "configure_logging", lambda: None)

    assert (
        chunker_cli.main(
            [
                "--source=/maps/cave.glb",
                "--analyze-chunk-sizes",
                "--analyze-workers=3",
            ]
        )
        == 0
    )

    output = capsys.readouterr().out
    assert received[0].source == "/maps/cave.glb"
    assert received[0].analyze_workers == "3"
    assert progress == [("reading source", 0.25)]
    assert finished == [True]
    assert "Recommended chunk size: 64" in output
    assert "Candidate scores:" in output
    assert "chunks=7" in output
    assert "p95_est=58.1 KiB" in output


def test_analyze_chunk_sizes_json_output_is_machine_readable(monkeypatch, capsys):
    monkeypatch.setattr(
        chunker_cli,
        "analyze_chunk_sizes",
        lambda _options: _recommendation(),
    )
    monkeypatch.setattr(
        chunker_cli,
        "compile_map",
        lambda _options: raise_assertion("analysis must not compile"),
    )

    assert (
        chunker_cli.main(
            ["--source=/maps/cave.glb", "--analyze-chunk-sizes", "--json"]
        )
        == 0
    )

    captured = capsys.readouterr()
    assert captured.err == ""
    payload = json.loads(captured.out)
    assert payload["recommended_size"] == 64.0
    assert payload["candidates"][1]["chunk_size"] == 64.0


def test_analyze_workers_requires_analysis_mode(capsys):
    assert (
        chunker_cli.main(["--source=/maps/cave.glb", "--analyze-workers=3"])
        == 2
    )

    captured = capsys.readouterr()
    assert captured.out == ""
    assert (
        "Error: --analyze-workers requires --analyze-chunk-sizes."
        in captured.err
    )


def raise_assertion(message: str):
    raise AssertionError(message)
