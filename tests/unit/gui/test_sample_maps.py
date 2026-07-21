"""Cover sample-map catalogs, downloads, cancellation, extraction, and cleanup."""

from __future__ import annotations

import json
import sys
import urllib.error
import zipfile
from pathlib import Path

import pytest

from caveviewer.gui import sample_maps
from caveviewer.gui.platform import DirectorySelection


class JsonResponse:
    def __init__(self, data):
        self._payload = json.dumps(data).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self):
        return self._payload


def test_known_sample_maps_include_current_public_release_assets():
    expected_assets = [
        "Boh.Yai.Mine.I.Low.Res.zip",
        "Boh.Yai.Mine.II.Low.Res.zip",
        "Devils.Eye.3D.Map.zip",
        "Peacock.Springs.Cave.System.3D.Map.zip",
    ]

    assert [sample.asset_name for sample in sample_maps.KNOWN_SAMPLE_MAPS] == expected_assets


def test_sample_maps_dirname_points_to_map_library():
    assert sample_maps.MAP_LIBRARY_DIRNAME == "map_library"
    assert sample_maps.SAMPLE_MAPS_DIRNAME == sample_maps.MAP_LIBRARY_DIRNAME


def test_catalog_populates_known_assets_and_keeps_missing_entries(monkeypatch):
    known = sample_maps.KNOWN_SAMPLE_MAPS[0]
    payload = {
        "assets": [
            {
                "name": known.asset_name,
                "browser_download_url": "https://example.invalid/map.zip",
                "size": 1234,
            }
        ]
    }
    monkeypatch.setattr(
        sample_maps.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: JsonResponse(payload),
    )
    sample_maps._SAMPLE_MAPS_CONFIG_LOGGED = False

    catalog, error = sample_maps.fetch_sample_map_catalog()

    assert error is None
    assert len(catalog) == len(sample_maps.KNOWN_SAMPLE_MAPS)
    assert catalog[0].download_url == "https://example.invalid/map.zip"
    assert catalog[0].size_bytes == 1234
    assert catalog[1].download_url is None
    assert sample_maps._SAMPLE_MAPS_CONFIG_LOGGED


@pytest.mark.parametrize(
    ("code", "message_fragment"),
    [(404, "No sample map release"), (500, "HTTP 500")],
)
def test_catalog_http_errors_keep_known_maps(monkeypatch, code, message_fragment):
    error = urllib.error.HTTPError("url", code, "failure", {}, None)
    monkeypatch.setattr(
        sample_maps.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(error),
    )

    catalog, message = sample_maps.fetch_sample_map_catalog()

    assert len(catalog) == len(sample_maps.KNOWN_SAMPLE_MAPS)
    assert all(item.download_url is None for item in catalog)
    assert message_fragment in (message or "")


@pytest.mark.parametrize(
    ("reason", "message_fragment"),
    [("DNS failure", "DNS failure"), (None, "Couldn't reach GitHub right now")],
)
def test_catalog_network_errors_are_actionable(monkeypatch, reason, message_fragment):
    monkeypatch.setattr(
        sample_maps.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            urllib.error.URLError(reason)
        ),
    )

    catalog, message = sample_maps.fetch_sample_map_catalog()

    assert len(catalog) == len(sample_maps.KNOWN_SAMPLE_MAPS)
    assert message_fragment in (message or "")


def test_catalog_rejects_malformed_json(monkeypatch):
    class BadJsonResponse(JsonResponse):
        def read(self):
            return b"{broken"

    monkeypatch.setattr(
        sample_maps.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: BadJsonResponse({}),
    )
    catalog, message = sample_maps.fetch_sample_map_catalog()
    assert len(catalog) == len(sample_maps.KNOWN_SAMPLE_MAPS)
    assert "unexpected response" in (message or "")


def test_sample_maps_container_avoids_duplicate_folder_name(tmp_path):
    container = tmp_path / sample_maps.SAMPLE_MAPS_DIRNAME
    sample = sample_maps.SampleMapInfo("Test Cave", "test.zip")
    assert sample_maps.local_sample_map_path(str(container), sample) == str(
        container / "Test Cave"
    )


def test_sample_map_paths_accept_portal_directory_selection(tmp_path):
    selection = DirectorySelection.from_path(str(tmp_path))
    sample = sample_maps.SampleMapInfo("Test Cave", "test.zip")

    assert sample_maps.local_sample_map_path(selection, sample) == str(
        tmp_path / sample_maps.SAMPLE_MAPS_DIRNAME / "Test Cave"
    )
    assert not sample_maps.is_sample_map_already_downloaded(selection, sample)


def test_default_sample_maps_install_dir_uses_xdg_data_home(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    data_home = tmp_path / "data"
    monkeypatch.setenv("XDG_DATA_HOME", str(data_home))

    install_dir = Path(sample_maps.default_sample_maps_install_dir())

    assert install_dir == data_home / "caveviewer" / sample_maps.SAMPLE_MAPS_DIRNAME
    assert install_dir.is_dir()
    assert sample_maps.local_sample_map_path(
        str(install_dir), sample_maps.SampleMapInfo("Test Cave", "test.zip")
    ) == str(install_dir / "Test Cave")


def test_default_sample_maps_install_dir_migrates_legacy_sample_maps_dir(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(sys, "platform", "linux")
    data_home = tmp_path / "data"
    monkeypatch.setenv("XDG_DATA_HOME", str(data_home))
    legacy_dir = data_home / "caveviewer" / sample_maps._LEGACY_SAMPLE_MAPS_DIRNAME
    legacy_dir.mkdir(parents=True)
    (legacy_dir / "marker.txt").write_text("legacy map", encoding="utf-8")

    install_dir = Path(sample_maps.default_sample_maps_install_dir())

    assert install_dir == data_home / "caveviewer" / sample_maps.MAP_LIBRARY_DIRNAME
    assert not legacy_dir.exists()
    assert (install_dir / "marker.txt").read_text(encoding="utf-8") == "legacy map"


def test_default_sample_maps_install_dir_merges_legacy_into_existing_map_library(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(sys, "platform", "linux")
    data_home = tmp_path / "data"
    monkeypatch.setenv("XDG_DATA_HOME", str(data_home))
    library_dir = data_home / "caveviewer" / sample_maps.MAP_LIBRARY_DIRNAME
    legacy_dir = data_home / "caveviewer" / sample_maps._LEGACY_SAMPLE_MAPS_DIRNAME
    library_dir.mkdir(parents=True)
    legacy_map = legacy_dir / "Test Cave"
    legacy_map.mkdir(parents=True)
    (legacy_map / "map.obj").write_text("legacy map", encoding="utf-8")

    install_dir = Path(sample_maps.default_sample_maps_install_dir())

    assert install_dir == library_dir
    assert not legacy_dir.exists()
    assert (library_dir / "Test Cave" / "map.obj").read_text(
        encoding="utf-8"
    ) == "legacy map"


def test_default_sample_maps_install_dir_does_not_overwrite_library_conflicts(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(sys, "platform", "linux")
    data_home = tmp_path / "data"
    monkeypatch.setenv("XDG_DATA_HOME", str(data_home))
    library_map = (
        data_home
        / "caveviewer"
        / sample_maps.MAP_LIBRARY_DIRNAME
        / "Test Cave"
    )
    legacy_map = (
        data_home
        / "caveviewer"
        / sample_maps._LEGACY_SAMPLE_MAPS_DIRNAME
        / "Test Cave"
    )
    library_map.mkdir(parents=True)
    legacy_map.mkdir(parents=True)
    (library_map / "map.obj").write_text("library map", encoding="utf-8")
    (legacy_map / "map.obj").write_text("legacy map", encoding="utf-8")
    (legacy_map / "texture.png").write_text("texture", encoding="utf-8")

    install_dir = Path(sample_maps.default_sample_maps_install_dir())

    assert install_dir == library_map.parent
    assert (library_map / "map.obj").read_text(encoding="utf-8") == "library map"
    assert (library_map / "texture.png").read_text(encoding="utf-8") == "texture"
    assert (legacy_map / "map.obj").read_text(encoding="utf-8") == "legacy map"


def test_default_sample_maps_install_dir_uses_legacy_when_map_library_is_file(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(sys, "platform", "linux")
    data_home = tmp_path / "data"
    monkeypatch.setenv("XDG_DATA_HOME", str(data_home))
    app_data_dir = data_home / "caveviewer"
    legacy_dir = app_data_dir / sample_maps._LEGACY_SAMPLE_MAPS_DIRNAME
    legacy_dir.mkdir(parents=True)
    (app_data_dir / sample_maps.MAP_LIBRARY_DIRNAME).write_text(
        "blocked", encoding="utf-8"
    )

    install_dir = Path(sample_maps.default_sample_maps_install_dir())

    assert install_dir == legacy_dir


def test_downloaded_state_and_existing_path_use_normal_location(tmp_path):
    sample = sample_maps.SampleMapInfo("Test Cave", "test.zip")
    expected = tmp_path / sample_maps.SAMPLE_MAPS_DIRNAME / "Test Cave"
    assert not sample_maps.is_sample_map_already_downloaded(str(tmp_path), sample)
    expected.mkdir(parents=True)
    assert not sample_maps.is_sample_map_already_downloaded(str(tmp_path), sample)
    (expected / "map.obj").write_text("mesh", encoding="utf-8")
    assert sample_maps.is_sample_map_already_downloaded(str(tmp_path), sample)
    assert sample_maps.existing_sample_map_path(str(tmp_path), sample) == str(expected)


def test_existing_path_supports_legacy_nested_location(tmp_path):
    container = tmp_path / sample_maps._LEGACY_SAMPLE_MAPS_DIRNAME
    sample = sample_maps.SampleMapInfo("Test Cave", "test.zip")
    legacy = container / sample_maps._LEGACY_SAMPLE_MAPS_DIRNAME / "Test Cave"
    legacy.mkdir(parents=True)
    (legacy / "map.obj").write_text("mesh", encoding="utf-8")
    assert sample_maps.is_sample_map_already_downloaded(str(container), sample)
    assert sample_maps.existing_sample_map_path(str(container), sample) == str(legacy)


def test_existing_path_supports_legacy_sibling_location(tmp_path):
    sample = sample_maps.SampleMapInfo("Test Cave", "test.zip")
    legacy = tmp_path / sample_maps._LEGACY_SAMPLE_MAPS_DIRNAME / "Test Cave"
    legacy.mkdir(parents=True)
    (legacy / "map.obj").write_text("mesh", encoding="utf-8")
    assert sample_maps.is_sample_map_already_downloaded(str(tmp_path), sample)
    assert sample_maps.existing_sample_map_path(str(tmp_path), sample) == str(legacy)


def test_app_supplied_sample_map_path_matches_managed_library_only(
    tmp_path, monkeypatch
):
    caveviewer_home = tmp_path / "caveviewer-home"
    monkeypatch.setenv("CAVEVIEWER_HOME", str(caveviewer_home))
    sample = sample_maps.KNOWN_SAMPLE_MAPS[0]
    managed_library_path = (
        caveviewer_home
        / "data"
        / sample_maps.MAP_LIBRARY_DIRNAME
        / sample.display_name
    )
    legacy_sample_path = (
        caveviewer_home
        / "data"
        / sample_maps._LEGACY_SAMPLE_MAPS_DIRNAME
        / sample.display_name
    )
    unrelated_user_path = (
        tmp_path
        / "user-maps"
        / sample_maps.MAP_LIBRARY_DIRNAME
        / sample.display_name
    )

    assert sample_maps.is_app_supplied_sample_map_path(managed_library_path)
    assert sample_maps.is_app_supplied_sample_map_path(legacy_sample_path)
    assert not sample_maps.is_app_supplied_sample_map_path(unrelated_user_path)


def test_folder_contents_handles_directory_read_error(tmp_path, monkeypatch):
    folder = tmp_path / "map"
    folder.mkdir()
    monkeypatch.setattr(
        sample_maps.os,
        "listdir",
        lambda _path: (_ for _ in ()).throw(PermissionError("denied")),
    )
    assert not sample_maps._folder_has_contents(str(folder))


@pytest.mark.parametrize("nested", [True, False])
def test_successful_sample_download_extracts_expected_layout(
    tmp_path, monkeypatch, nested
):
    sample = sample_maps.SampleMapInfo(
        "Test Cave", "test.zip", "https://example.invalid/test.zip", None
    )
    destination = tmp_path / sample_maps.SAMPLE_MAPS_DIRNAME / "Test Cave"
    destination.mkdir(parents=True)
    (destination / "obsolete.txt").write_text("old", encoding="utf-8")

    def create_zip(_url, _size, zip_path, progress_cb=None, cancel_cb=None):
        assert cancel_cb is None or not cancel_cb()
        prefix = "archive-root/" if nested else ""
        with zipfile.ZipFile(zip_path, "w") as archive:
            archive.writestr(prefix + "map.obj", "new mesh")
            archive.writestr(prefix + "map.mtl", "newmtl rock")
        if progress_cb:
            progress_cb(1, 1)

    progress = []
    monkeypatch.setattr(sample_maps, "download_update", create_zip)

    result = sample_maps.download_and_extract_sample_map(
        str(tmp_path), sample, progress_cb=lambda done, total: progress.append((done, total))
    )

    assert result == str(destination)
    assert (destination / "map.obj").read_text(encoding="utf-8") == "new mesh"
    assert not (destination / "obsolete.txt").exists()
    assert not list(destination.parent.glob(".Test-Cave.tmp-*"))
    assert not list(destination.parent.glob(".Test-Cave.tmp-*.previous"))
    assert progress == [(1, 1)]


def test_sample_publish_copy_failure_preserves_existing_install_and_cleans_staging(
    tmp_path, monkeypatch
):
    sample = sample_maps.SampleMapInfo(
        "Test Cave", "test.zip", "https://example.invalid/test.zip", None
    )
    destination = tmp_path / sample_maps.SAMPLE_MAPS_DIRNAME / "Test Cave"
    destination.mkdir(parents=True)
    marker = destination / "map.obj"
    marker.write_text("existing map", encoding="utf-8")

    def create_zip(_url, _size, zip_path, progress_cb=None, cancel_cb=None):
        with zipfile.ZipFile(zip_path, "w") as archive:
            archive.writestr("map.obj", "new mesh")
            archive.writestr("map.mtl", "newmtl rock")

    def fail_copytree(_source, dest, *args, **kwargs):
        del args, kwargs
        Path(dest, "partial.obj").write_text("partial", encoding="utf-8")
        raise OSError("copy failed")

    monkeypatch.setattr(sample_maps, "download_update", create_zip)
    monkeypatch.setattr(sample_maps.shutil, "copytree", fail_copytree)

    with pytest.raises(OSError, match="copy failed"):
        sample_maps.download_and_extract_sample_map(str(tmp_path), sample)

    assert marker.read_text(encoding="utf-8") == "existing map"
    assert not list(destination.parent.glob(".Test-Cave.tmp-*"))


def test_sample_publish_failure_restores_existing_install_and_cleans_staging(
    tmp_path, monkeypatch
):
    sample = sample_maps.SampleMapInfo(
        "Test Cave", "test.zip", "https://example.invalid/test.zip", None
    )
    destination = tmp_path / sample_maps.SAMPLE_MAPS_DIRNAME / "Test Cave"
    destination.mkdir(parents=True)
    marker = destination / "map.obj"
    marker.write_text("existing map", encoding="utf-8")

    def create_zip(_url, _size, zip_path, progress_cb=None, cancel_cb=None):
        with zipfile.ZipFile(zip_path, "w") as archive:
            archive.writestr("map.obj", "new mesh")
            archive.writestr("map.mtl", "newmtl rock")

    real_replace = sample_maps.os.replace

    def fail_new_publish(source, dest):
        if (
            Path(source).name.startswith(".Test-Cave.tmp-")
            and not Path(source).name.endswith(".previous")
            and Path(dest) == destination
        ):
            raise OSError("publish failed")
        real_replace(source, dest)

    monkeypatch.setattr(sample_maps, "download_update", create_zip)
    monkeypatch.setattr(sample_maps.os, "replace", fail_new_publish)

    with pytest.raises(OSError, match="publish failed"):
        sample_maps.download_and_extract_sample_map(str(tmp_path), sample)

    assert marker.read_text(encoding="utf-8") == "existing map"
    assert not list(destination.parent.glob(".Test-Cave.tmp-*"))
    assert not list(destination.parent.glob(".Test-Cave.tmp-*.previous"))


def test_sample_download_accepts_portal_directory_selection(tmp_path, monkeypatch):
    sample = sample_maps.SampleMapInfo(
        "Test Cave", "test.zip", "https://example.invalid/test.zip", None
    )

    def create_zip(_url, _size, zip_path, progress_cb=None, cancel_cb=None):
        assert cancel_cb is None or not cancel_cb()
        with zipfile.ZipFile(zip_path, "w") as archive:
            archive.writestr("map.obj", "mesh")
            archive.writestr("map.mtl", "newmtl rock")

    monkeypatch.setattr(sample_maps, "download_update", create_zip)

    result = sample_maps.download_and_extract_sample_map(
        DirectorySelection.from_path(str(tmp_path)),
        sample,
    )

    assert result == str(tmp_path / sample_maps.SAMPLE_MAPS_DIRNAME / "Test Cave")
    assert Path(result, "map.obj").read_text(encoding="utf-8") == "mesh"


def test_cancelled_sample_download_removes_temporary_files_and_preserves_install(
    tmp_path, monkeypatch
):
    sample = sample_maps.SampleMapInfo(
        "Test Cave", "test.zip", "https://example.invalid/test.zip", None
    )
    destination = Path(sample_maps.local_sample_map_path(str(tmp_path), sample))
    destination.mkdir(parents=True)
    marker = destination / "map.obj"
    marker.write_text("existing map", encoding="utf-8")
    temp_root = tmp_path / "temporary-downloads"
    temp_root.mkdir()
    monkeypatch.setattr(sample_maps.tempfile, "tempdir", str(temp_root))
    cancel_requested = [False]
    partial_paths = []

    def cancel_partial_download(
        _url, _size, zip_path, progress_cb=None, cancel_cb=None
    ):
        partial_path = Path(zip_path)
        partial_path.write_bytes(b"partial map archive")
        partial_paths.append(partial_path)
        cancel_requested[0] = True
        assert cancel_cb is not None and cancel_cb()
        raise sample_maps.DownloadCancelled("cancelled")

    monkeypatch.setattr(sample_maps, "download_update", cancel_partial_download)

    with pytest.raises(sample_maps.DownloadCancelled):
        sample_maps.download_and_extract_sample_map(
            str(tmp_path),
            sample,
            cancel_cb=lambda: cancel_requested[0],
        )

    assert marker.read_text(encoding="utf-8") == "existing map"
    assert partial_paths and not partial_paths[0].exists()
    assert list(temp_root.iterdir()) == []


def test_retry_after_cancellation_starts_a_fresh_sample_download(
    tmp_path, monkeypatch
):
    sample = sample_maps.SampleMapInfo(
        "Test Cave", "test.zip", "https://example.invalid/test.zip", None
    )
    cancel_requested = [False]
    first_zip_path = [None]

    def cancel_first_download(
        _url, _size, zip_path, progress_cb=None, cancel_cb=None
    ):
        first_zip_path[0] = Path(zip_path)
        first_zip_path[0].write_bytes(b"partial")
        cancel_requested[0] = True
        raise sample_maps.DownloadCancelled("cancelled")

    monkeypatch.setattr(sample_maps, "download_update", cancel_first_download)
    with pytest.raises(sample_maps.DownloadCancelled):
        sample_maps.download_and_extract_sample_map(
            str(tmp_path),
            sample,
            cancel_cb=lambda: cancel_requested[0],
        )

    cancel_requested[0] = False

    def complete_fresh_download(
        _url, _size, zip_path, progress_cb=None, cancel_cb=None
    ):
        assert Path(zip_path) != first_zip_path[0]
        assert first_zip_path[0] is not None and not first_zip_path[0].exists()
        assert cancel_cb is not None and not cancel_cb()
        with zipfile.ZipFile(zip_path, "w") as archive:
            archive.writestr("map.obj", "complete map")
            archive.writestr("map.mtl", "newmtl rock")

    monkeypatch.setattr(sample_maps, "download_update", complete_fresh_download)
    result = sample_maps.download_and_extract_sample_map(
        str(tmp_path),
        sample,
        cancel_cb=lambda: cancel_requested[0],
    )

    assert Path(result, "map.obj").read_text(encoding="utf-8") == "complete map"
