"""Tests for GUI map-opening workflow helpers."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from caveviewer.gui import map_opening
from caveviewer.gui.features import FeatureDecision, FeatureId, FeatureState
from caveviewer.gui.platform import DesktopServiceError


def test_pick_folder_dialog_uses_desktop_services_and_destroys_root(monkeypatch):
    calls = []

    class FakeRoot:
        def withdraw(self):
            calls.append("withdraw")

        def destroy(self):
            calls.append("destroy")

    monkeypatch.setattr(
        map_opening,
        "_hidden_tk_root",
        lambda: calls.append("root") or FakeRoot(),
    )

    class FakeDesktopServices:
        def choose_directory(self, **options):
            calls.append(("choose_directory", options))
            return SimpleNamespace(path="/maps/cave")

    assert (
        map_opening.pick_folder_dialog(desktop_services=FakeDesktopServices())
        == "/maps/cave"
    )
    assert calls[0] == "root"
    assert calls[1][0] == "choose_directory"
    assert calls[1][1]["title"] == "Open Map Folder"
    assert isinstance(calls[1][1]["parent"], FakeRoot)
    assert calls[2] == "destroy"


def test_pick_folder_dialog_rejects_unavailable_service_before_creating_tk_root(
    monkeypatch,
):
    monkeypatch.setattr(
        map_opening,
        "_hidden_tk_root",
        lambda: pytest.fail("disabled directory selection must not create Tk"),
    )

    with pytest.raises(
        DesktopServiceError,
        match="Directory selection is unavailable",
    ):
        map_opening.pick_folder_dialog(desktop_services=object())


def test_pick_folder_dialog_rechecks_the_injected_runtime_for_each_action(
    monkeypatch,
):
    calls = []
    preflight_calls = []

    class FakeRoot:
        def destroy(self):
            calls.append("destroy")

    monkeypatch.setattr(
        map_opening,
        "_hidden_tk_root",
        lambda: calls.append("root") or FakeRoot(),
    )
    monkeypatch.setattr(
        map_opening,
        "probe_directory_selection",
        lambda _services: pytest.fail("runtime preflight must be reused"),
    )

    class FakeDesktopServices:
        def choose_directory(self, **options):
            calls.append(("choose_directory", options))
            return SimpleNamespace(path="/maps/cave")

    desktop_services = FakeDesktopServices()

    class FakeRuntime:
        def __init__(self):
            self.desktop_services = desktop_services

        def directory_selection_preflight(self):
            preflight_calls.append(True)
            return SimpleNamespace(
                decision=FeatureDecision(
                    feature=FeatureId.DIRECTORY_SELECTION,
                    state=FeatureState.ENABLED,
                    reason_code="directory_selection_available",
                    explanation="Directory selection is available.",
                    route="portal_then_tk",
                )
            )

    runtime = FakeRuntime()

    assert map_opening.pick_folder_dialog(platform_runtime=runtime) == "/maps/cave"
    assert map_opening.pick_folder_dialog(platform_runtime=runtime) == "/maps/cave"
    assert preflight_calls == [True, True]
    assert [call for call in calls if call == "root"] == ["root", "root"]
    assert len([call for call in calls if call == "destroy"]) == 2
    assert len([call for call in calls if call[0] == "choose_directory"]) == 2


def test_resolve_selected_map_folder_returns_model_target(monkeypatch, tmp_path):
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    descriptor = {"format": "glb", "glb_path": str(source_dir / "map.glb")}
    monkeypatch.setattr(
        map_opening.source_model,
        "find_model_file",
        lambda folder, *, logger=None: descriptor,
    )

    target = map_opening.resolve_selected_map_folder(str(source_dir))

    assert target.source_dir == str(source_dir)
    assert target.map_name == "map.glb"
    assert target.model_descriptor is descriptor
    assert target.textures_dir == str(source_dir)
    assert not target.is_prebuilt_cache


def test_resolve_selected_map_folder_enforces_source_format_policy(monkeypatch, tmp_path):
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    descriptor = {"format": "ply", "ply_path": str(source_dir / "map.ply")}
    monkeypatch.setattr(
        map_opening.source_model,
        "find_model_file",
        lambda folder, *, logger=None: descriptor,
    )

    with pytest.raises(FileNotFoundError, match="not supported"):
        map_opening.resolve_selected_map_folder(str(source_dir))


def test_resolve_selected_map_folder_returns_prebuilt_cache(monkeypatch, tmp_path):
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    (cache_dir / map_opening.chunker.MANIFEST_NAME).write_text("{}", encoding="utf-8")
    manifest = {"source_obj": "/maps/devils-eye.obj"}

    def fail_find_model_file(folder, *, logger=None):
        raise FileNotFoundError("no model")

    monkeypatch.setattr(map_opening.source_model, "find_model_file", fail_find_model_file)
    monkeypatch.setattr(map_opening.chunker, "load_manifest", lambda folder: manifest)

    target = map_opening.resolve_selected_map_folder(str(cache_dir))

    assert target.source_dir == str(cache_dir)
    assert target.map_name == "devils-eye.obj"
    assert target.cache_dir == str(cache_dir)
    assert target.textures_dir == str(cache_dir)
    assert target.manifest is manifest
    assert target.is_prebuilt_cache


def test_resolve_selected_map_folder_reraises_model_error_without_cache(
    monkeypatch, tmp_path
):
    source_dir = tmp_path / "empty"
    source_dir.mkdir()
    error = FileNotFoundError("no supported model")

    def fail_find_model_file(folder, *, logger=None):
        raise error

    monkeypatch.setattr(map_opening.source_model, "find_model_file", fail_find_model_file)

    with pytest.raises(FileNotFoundError) as exc_info:
        map_opening.resolve_selected_map_folder(str(source_dir))

    assert exc_info.value is error
