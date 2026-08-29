"""Tests for bounded, portable preference-file transfer."""

from __future__ import annotations

import json
import os

import pytest

from caveviewer.core.preferences import transfer
from caveviewer.core.preferences.schema import (
    Preferences,
    preference_defaults,
)


def _preferences(**updates: str) -> Preferences:
    values = preference_defaults()
    values.update(updates)
    return Preferences(values)


def test_portable_preferences_round_trip_all_declared_values(tmp_path):
    unicode_directory = tmp_path / "cavés"
    unicode_directory.mkdir()
    preferences = _preferences(
        io_workers="7",
        recording_dir=str(unicode_directory),
    )

    document = transfer.encode_preferences(preferences)
    result = transfer.decode_preferences(document)

    assert result.preferences == preferences
    assert result.defaulted_keys == ()
    assert result.ignored_keys == ()
    assert "cavés" in document.decode("utf-8")


def test_import_defaults_only_missing_and_invalid_fields():
    defaults = preference_defaults()
    document = json.dumps(
        {
            "io_workers": "999",
            "upload_chunks_per_frame": "3",
        }
    ).encode("utf-8")

    result = transfer.decode_preferences(document)

    assert result.preferences["io_workers"] == defaults["io_workers"]
    assert result.preferences["upload_chunks_per_frame"] == "3"
    assert "io_workers" in result.defaulted_keys
    assert "upload_chunks_per_frame" not in result.defaulted_keys


def test_import_ignores_unknown_fields():
    document = json.dumps({"future_setting": "enabled"}).encode("utf-8")

    result = transfer.decode_preferences(document)

    assert result.preferences == preference_defaults()
    assert result.ignored_keys == ("future_setting",)


@pytest.mark.parametrize("document", [b"{broken", b"[]", b"null", b'"text"'])
def test_import_rejects_malformed_or_non_object_documents(document):
    with pytest.raises(transfer.PreferencesTransferError):
        transfer.decode_preferences(document)


def test_import_rejects_oversized_document_before_parsing():
    with pytest.raises(transfer.PreferencesTransferError, match="larger"):
        transfer.decode_preferences(b" " * 17, max_bytes=16)


def test_file_round_trip_is_bounded_and_atomic(tmp_path):
    path = tmp_path / transfer.PREFERENCES_EXPORT_FILENAME
    preferences = _preferences(io_workers="6")

    transfer.save_preferences_file(path, preferences)
    result = transfer.load_preferences_file(path)

    assert result.preferences == preferences
    assert not list(tmp_path.glob("*.tmp"))


def test_export_failure_preserves_existing_file_and_cleans_staging(
    tmp_path,
    monkeypatch,
):
    path = tmp_path / transfer.PREFERENCES_EXPORT_FILENAME
    path.write_text("existing", encoding="utf-8")

    def fail_replace(*_args):
        raise OSError("replace failed")

    monkeypatch.setattr(transfer.os, "replace", fail_replace)

    with pytest.raises(transfer.PreferencesTransferError):
        transfer.save_preferences_file(path, _preferences())

    assert path.read_text(encoding="utf-8") == "existing"
    assert not list(tmp_path.glob("*.tmp"))


def test_load_rejects_file_larger_than_bound(tmp_path):
    path = tmp_path / transfer.PREFERENCES_EXPORT_FILENAME
    path.write_bytes(b" " * 17)

    with pytest.raises(transfer.PreferencesTransferError, match="larger"):
        transfer.load_preferences_file(path, max_bytes=16)
