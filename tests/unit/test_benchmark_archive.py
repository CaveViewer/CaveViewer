"""Security contracts for benchmark archive preparation."""

from __future__ import annotations

import importlib.util
import io
import stat
import sys
import zipfile
from pathlib import Path

import pytest


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPOSITORY_ROOT / "scripts" / "benchmark" / "prepare_benchmark_archive.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("prepare_benchmark_archive", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _write_zip(path: Path, entries: list[tuple[zipfile.ZipInfo | str, bytes]]) -> None:
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, payload in entries:
            archive.writestr(name, payload)


def test_safe_archive_extracts_regular_files(tmp_path: Path):
    module = _load_module()
    archive = tmp_path / "map.zip"
    destination = tmp_path / "map"
    _write_zip(archive, [("cache/manifest.json", b"{}"), ("cache/chunk.bin", b"data")])

    module.extract_archive(archive, destination)

    assert (destination / "cache/manifest.json").read_bytes() == b"{}"
    assert (destination / "cache/chunk.bin").read_bytes() == b"data"


@pytest.mark.parametrize("member_name", ("../escape", "/absolute", "dir\\escape"))
def test_archive_rejects_unsafe_paths(tmp_path: Path, member_name: str):
    module = _load_module()
    archive = tmp_path / "map.zip"
    _write_zip(archive, [(member_name, b"unsafe")])

    with pytest.raises(ValueError, match="unsafe path|backslash path"):
        module.extract_archive(archive, tmp_path / "map")


@pytest.mark.parametrize("file_type", (stat.S_IFLNK, stat.S_IFCHR, stat.S_IFIFO))
def test_archive_rejects_links_and_special_files(tmp_path: Path, file_type: int):
    module = _load_module()
    archive = tmp_path / "map.zip"
    member = zipfile.ZipInfo("unsafe-entry")
    member.create_system = 3
    member.external_attr = (file_type | 0o600) << 16
    _write_zip(archive, [(member, b"payload")])

    with pytest.raises(ValueError, match="not a regular file or directory"):
        module.extract_archive(archive, tmp_path / "map")


def test_archive_rejects_duplicate_members(tmp_path: Path):
    module = _load_module()
    archive = tmp_path / "map.zip"
    _write_zip(archive, [("duplicate", b"one"), ("duplicate", b"two")])

    with pytest.raises(ValueError, match="duplicate archive member"):
        module.extract_archive(archive, tmp_path / "map")


def test_archive_rejects_expansion_and_compression_bombs(tmp_path: Path, monkeypatch):
    module = _load_module()
    expanded_archive = tmp_path / "expanded.zip"
    _write_zip(expanded_archive, [("large", b"a" * 20)])
    monkeypatch.setattr(module, "MAX_EXPANDED_BYTES", 10)
    with pytest.raises(ValueError, match="expanded-size limit"):
        module.extract_archive(expanded_archive, tmp_path / "expanded")

    module = _load_module()
    ratio_archive = tmp_path / "ratio.zip"
    _write_zip(ratio_archive, [("ratio", b"\0" * 100_000)])
    monkeypatch.setattr(module, "MAX_COMPRESSION_RATIO", 2)
    with pytest.raises(ValueError, match="compression-ratio limit"):
        module.extract_archive(ratio_archive, tmp_path / "ratio")


def test_download_requires_hash_before_network_access(tmp_path: Path, monkeypatch):
    module = _load_module()

    def fail_network(*_args, **_kwargs):
        raise AssertionError("network must not be reached")

    monkeypatch.setattr(module.urllib.request, "urlopen", fail_network)
    with pytest.raises(ValueError, match="exactly 64 hexadecimal"):
        module.download_archive(
            "https://example.invalid/map.zip",
            "",
            tmp_path / "map.zip",
        )


def test_download_stream_enforces_compressed_size_limit(tmp_path: Path, monkeypatch):
    module = _load_module()

    class Response(io.BytesIO):
        headers = {}

        def geturl(self):
            return "https://example.invalid/map.zip"

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            self.close()

    monkeypatch.setattr(module, "MAX_DOWNLOAD_BYTES", 3)
    monkeypatch.setattr(
        module.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: Response(b"four"),
    )
    with pytest.raises(ValueError, match="compressed-size limit"):
        module.download_archive(
            "https://example.invalid/map.zip",
            "0" * 64,
            tmp_path / "map.zip",
        )


@pytest.mark.parametrize(
    "url",
    (
        "http://example.invalid/map.zip",
        "https://user:password@example.invalid/map.zip",
    ),
)
def test_download_rejects_unsafe_urls_before_network_access(
    tmp_path: Path,
    monkeypatch,
    url: str,
):
    module = _load_module()

    def fail_network(*_args, **_kwargs):
        raise AssertionError("network must not be reached")

    monkeypatch.setattr(module.urllib.request, "urlopen", fail_network)
    with pytest.raises(ValueError, match="HTTPS|credentials"):
        module.download_archive(url, "0" * 64, tmp_path / "map.zip")
