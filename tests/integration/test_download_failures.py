"""Exercise cleanup behavior for interrupted update and map-library downloads."""

from __future__ import annotations

import hashlib
import urllib.error
import zipfile

import pytest

from caveviewer.gui import standard_library_maps, update_checker


class BytesResponse:
    def __init__(self, payload: bytes):
        self._payload = payload
        self._sent = False
        self.headers = {"Content-Length": str(len(payload))}

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self, _size=-1):
        if self._sent:
            return b""
        self._sent = True
        return self._payload


class InterruptedResponse(BytesResponse):
    def read(self, _size=-1):
        if not self._sent:
            self._sent = True
            return self._payload
        raise urllib.error.URLError("connection reset")


@pytest.mark.integration
def test_update_download_network_failure_leaves_no_file(tmp_path, monkeypatch):
    destination = tmp_path / "downloads" / "update.zip"
    monkeypatch.setattr(
        update_checker.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            urllib.error.URLError("offline")
        ),
    )

    with pytest.raises(urllib.error.URLError):
        update_checker.download_update(
            "https://invalid.example/update.zip", None, str(destination)
        )

    assert not destination.exists()


@pytest.mark.integration
def test_network_failure_does_not_delete_preexisting_destination(tmp_path, monkeypatch):
    destination = tmp_path / "downloads" / "update.zip"
    destination.parent.mkdir()
    destination.write_bytes(b"previous complete download")
    monkeypatch.setattr(
        update_checker.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            urllib.error.URLError("offline")
        ),
    )

    with pytest.raises(urllib.error.URLError):
        update_checker.download_update(
            "https://invalid.example/update.zip", None, str(destination)
        )

    assert destination.read_bytes() == b"previous complete download"


@pytest.mark.integration
def test_interrupted_update_download_removes_partial_file(tmp_path, monkeypatch):
    destination = tmp_path / "downloads" / "update.zip"
    monkeypatch.setattr(
        update_checker.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: InterruptedResponse(b"partial"),
    )

    with pytest.raises(urllib.error.URLError):
        update_checker.download_update(
            "https://invalid.example/update.zip", None, str(destination)
        )

    assert not destination.exists()


@pytest.mark.integration
def test_cancelled_download_removes_partial_and_retry_does_not_resume(
    tmp_path, monkeypatch
):
    destination = tmp_path / "downloads" / "map-library.zip"
    responses = iter([BytesResponse(b"partial"), BytesResponse(b"complete")])
    requests = []

    def open_response(request, **_kwargs):
        requests.append(request)
        return next(responses)

    monkeypatch.setattr(update_checker.urllib.request, "urlopen", open_response)
    cancel_requested = [False]

    def request_cancellation(_downloaded, _total):
        cancel_requested[0] = True

    with pytest.raises(update_checker.DownloadCancelled):
        update_checker.download_update(
            "https://invalid.example/map-library.zip",
            None,
            str(destination),
            progress_cb=request_cancellation,
            cancel_cb=lambda: cancel_requested[0],
        )

    assert not destination.exists()

    cancel_requested[0] = False
    update_checker.download_update(
        "https://invalid.example/map-library.zip",
        len(b"complete"),
        str(destination),
        cancel_cb=lambda: cancel_requested[0],
    )

    assert destination.read_bytes() == b"complete"
    assert len(requests) == 2
    assert all(request.get_header("Range") is None for request in requests)


@pytest.mark.integration
def test_size_mismatch_removes_downloaded_file(tmp_path, monkeypatch):
    destination = tmp_path / "downloads" / "update.zip"
    monkeypatch.setattr(
        update_checker.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: BytesResponse(b"short"),
    )

    with pytest.raises(IOError, match="file size"):
        update_checker.download_update(
            "https://invalid.example/update.zip", 100, str(destination)
        )

    assert not destination.exists()


@pytest.mark.integration
def test_hash_mismatch_removes_downloaded_file(tmp_path, monkeypatch):
    destination = tmp_path / "downloads" / "update.zip"
    monkeypatch.setattr(
        update_checker.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: BytesResponse(b"payload"),
    )

    with pytest.raises(IOError, match="hash"):
        update_checker.download_update(
            "https://invalid.example/update.zip",
            7,
            str(destination),
            expected_sha256="0" * 64,
        )

    assert not destination.exists()


@pytest.mark.integration
def test_update_download_reports_verification_phase(tmp_path, monkeypatch):
    payload = b"verified payload"
    destination = tmp_path / "downloads" / "update.zip"
    phases = []
    progress = []
    monkeypatch.setattr(
        update_checker.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: BytesResponse(payload),
    )

    update_checker.download_update(
        "https://invalid.example/update.zip",
        len(payload),
        str(destination),
        expected_sha256=hashlib.sha256(payload).hexdigest(),
        progress_cb=lambda downloaded, total: progress.append((downloaded, total)),
        phase_cb=phases.append,
    )

    assert progress == [(len(payload), len(payload))]
    assert phases == ["verifying"]
    assert destination.read_bytes() == payload


@pytest.mark.integration
def test_cancellation_at_verification_phase_removes_download(tmp_path, monkeypatch):
    payload = b"cancel before verification"
    destination = tmp_path / "downloads" / "update.zip"
    cancel_requested = [False]
    monkeypatch.setattr(
        update_checker.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: BytesResponse(payload),
    )

    with pytest.raises(update_checker.DownloadCancelled):
        update_checker.download_update(
            "https://invalid.example/update.zip",
            len(payload),
            str(destination),
            expected_sha256=hashlib.sha256(payload).hexdigest(),
            cancel_cb=lambda: cancel_requested[0],
            phase_cb=lambda _phase: cancel_requested.__setitem__(0, True),
        )

    assert not destination.exists()


@pytest.mark.integration
def test_standard_library_map_without_download_url_is_rejected(tmp_path):
    sample = standard_library_maps.StandardLibraryMapInfo("Test Cave", "test.zip")
    with pytest.raises(ValueError, match="No download URL"):
        standard_library_maps.download_and_extract_standard_library_map(str(tmp_path), sample)


@pytest.mark.integration
def test_failed_standard_library_download_preserves_existing_install(tmp_path, monkeypatch):
    sample = standard_library_maps.StandardLibraryMapInfo(
        "Test Cave", "test.zip", "https://invalid.example/test.zip", 50
    )
    destination = standard_library_maps.local_standard_library_map_path(str(tmp_path), sample)
    marker = tmp_path / sample.display_name / "map.obj"
    marker.parent.mkdir(parents=True)
    marker.write_text("existing map", encoding="utf-8")

    def fail_download(*_args, **_kwargs):
        raise urllib.error.URLError("offline")

    monkeypatch.setattr(
        standard_library_maps,
        "download_standard_library_map_archive",
        fail_download,
    )

    with pytest.raises(urllib.error.URLError):
        standard_library_maps.download_and_extract_standard_library_map(str(tmp_path), sample)

    assert marker.read_text(encoding="utf-8") == "existing map"
    assert destination == str(marker.parent)


@pytest.mark.integration
def test_corrupt_standard_library_zip_preserves_existing_install(tmp_path, monkeypatch):
    sample = standard_library_maps.StandardLibraryMapInfo(
        "Test Cave", "test.zip", "https://invalid.example/test.zip", None
    )
    marker = tmp_path / sample.display_name / "map.obj"
    marker.parent.mkdir(parents=True)
    marker.write_text("existing map", encoding="utf-8")

    def write_corrupt_zip(_url, _size, destination, **_kwargs):
        with open(destination, "wb") as file_obj:
            file_obj.write(b"not a zip archive")

    monkeypatch.setattr(
        standard_library_maps,
        "download_standard_library_map_archive",
        write_corrupt_zip,
    )

    with pytest.raises(zipfile.BadZipFile):
        standard_library_maps.download_and_extract_standard_library_map(str(tmp_path), sample)

    assert marker.read_text(encoding="utf-8") == "existing map"
