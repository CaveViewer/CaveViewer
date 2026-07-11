from __future__ import annotations

import json
import urllib.error

import pytest

from caveviewer.gui import update_checker
from caveviewer.gui.update_signature import SignatureVerificationError


class FakePlatformAdapter:
    def __init__(self, channel="windows_app", supported=True):
        self.channel = channel
        self.supported = supported

    def install_channel(self):
        return self.channel

    def supports_install_channel(self, channel):
        return self.supported

    def unsupported_install_channel_message(self, channel):
        return f"Unsupported install channel '{channel}'."

    def channel_download_url_keys(self, channel):
        return (f"{channel}_url", "download_url")

    def channel_download_size_keys(self, channel):
        return (f"{channel}_size", "download_size_bytes")

    def channel_sha256_keys(self, channel):
        return (f"{channel}_sha256", "sha256")

    def missing_download_url_message(self, channel):
        return f"Missing download URL for {channel}."

    def detect_package_kind(self, download_url, channel):
        lowered = (download_url or "").lower()
        for suffix in ("tar.gz", "appimage", "dmg", "pkg", "zip", "msi", "exe"):
            if lowered.endswith("." + suffix):
                return suffix
        return "unknown"

    def update_check_user_agent(self):
        return "CaveViewer-Test"


@pytest.fixture
def configured_update_checker(monkeypatch):
    adapter = FakePlatformAdapter()
    monkeypatch.setattr(update_checker, "_PLATFORM_ADAPTER", adapter)
    monkeypatch.setattr(update_checker, "_MANIFEST_URL", "https://example.invalid/stable.json")
    monkeypatch.setattr(update_checker, "_MANIFEST_SIGNATURE_URL", "https://example.invalid/stable.json.sig")
    return adapter


def _set_manifest(monkeypatch, data):
    payload = json.dumps(data).encode("utf-8")
    monkeypatch.setattr(
        update_checker,
        "_fetch_url_bytes",
        lambda url, headers, timeout: payload,
    )
    return payload


@pytest.mark.parametrize(
    ("text", "expected"),
    [("1.2.3", (1, 2, 3)), ("v2.10", (2, 10)), ("bad", (0,)), ("1.beta", (0,))],
)
def test_parse_version(text, expected):
    assert update_checker._parse_version(text) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [(None, None), ("12", 12), (5, 5), ("bad", None), (object(), None)],
)
def test_parse_optional_int(value, expected):
    assert update_checker._parse_optional_int(value) == expected


def test_manifest_value_helpers_use_first_valid_alias():
    data = {"old": " ", "new": " value ", "bad_size": "x", "size": "42"}
    assert update_checker._first_non_empty_str(data, ("old", "new")) == "value"
    assert update_checker._first_optional_int(data, ("bad_size", "size")) == 42


def test_update_check_reports_unconfigured_manifest(monkeypatch):
    monkeypatch.setattr(update_checker, "_MANIFEST_URL", "")
    result = update_checker.check_for_update("1.0.0")
    assert not result.update_available
    assert "isn't configured" in (result.error or "")


@pytest.mark.parametrize("code", [404, 500])
def test_update_check_handles_manifest_http_errors(
    configured_update_checker, monkeypatch, code
):
    error = urllib.error.HTTPError("url", code, "failure", {}, None)
    monkeypatch.setattr(
        update_checker,
        "_fetch_url_bytes",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(error),
    )
    result = update_checker.check_for_update("1.0.0")
    assert not result.update_available
    assert str(code) in (result.error or "")


def test_update_check_handles_network_error(configured_update_checker, monkeypatch):
    monkeypatch.setattr(
        update_checker,
        "_fetch_url_bytes",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            urllib.error.URLError("offline")
        ),
    )
    result = update_checker.check_for_update("1.0.0")
    assert not result.update_available
    assert "Couldn't reach" in (result.error or "")


def test_update_check_handles_invalid_json(configured_update_checker, monkeypatch):
    monkeypatch.setattr(
        update_checker, "_fetch_url_bytes", lambda *_args, **_kwargs: b"{broken"
    )
    result = update_checker.check_for_update("1.0.0")
    assert not result.update_available
    assert "unexpected update manifest format" in (result.error or "")


def test_update_check_rejects_unsupported_channel(
    configured_update_checker, monkeypatch
):
    configured_update_checker.supported = False
    _set_manifest(monkeypatch, {"latest_version": "2.0.0"})
    result = update_checker.check_for_update("1.0.0")
    assert not result.update_available
    assert "Unsupported install channel" in (result.error or "")


def test_update_check_rejects_wrong_payload_type(
    configured_update_checker, monkeypatch
):
    _set_manifest(
        monkeypatch,
        {"latest_version": "2.0.0", "windows_app_url": "https://x/update.dmg"},
    )
    result = update_checker.check_for_update("1.0.0")
    assert not result.update_available
    assert "not valid" in (result.error or "")


def test_update_check_rejects_missing_version(monkeypatch, configured_update_checker):
    configured_update_checker.channel = "custom"
    _set_manifest(monkeypatch, {"download_url": "https://x/update.bin"})
    result = update_checker.check_for_update("1.0.0")
    assert not result.update_available
    assert "latest_version" in (result.error or "")


def test_update_check_rejects_missing_download_url(
    monkeypatch, configured_update_checker
):
    configured_update_checker.channel = "custom"
    _set_manifest(monkeypatch, {"latest_version": "2.0.0"})
    result = update_checker.check_for_update("1.0.0")
    assert not result.update_available
    assert "Missing download URL" in (result.error or "")


def test_current_version_does_not_require_signature(
    configured_update_checker, monkeypatch
):
    _set_manifest(
        monkeypatch,
        {
            "latest_version": "1.0.0",
            "windows_app_url": "https://x/update.zip",
            "release_notes": "Already current",
        },
    )
    monkeypatch.setattr(
        update_checker,
        "_verify_manifest_signature_required",
        lambda _payload: (_ for _ in ()).throw(AssertionError("must not verify")),
    )
    result = update_checker.check_for_update("1.0.0")
    assert not result.update_available
    assert result.latest_version == "1.0.0"
    assert result.release_notes == "Already current"


def test_newer_update_requires_valid_signature(
    configured_update_checker, monkeypatch
):
    _set_manifest(
        monkeypatch,
        {"latest_version": "2.0.0", "windows_app_url": "https://x/update.zip"},
    )
    monkeypatch.setattr(
        update_checker, "_verify_manifest_signature_required", lambda _payload: False
    )
    result = update_checker.check_for_update("1.0.0")
    assert not result.update_available
    assert "signature" in (result.error or "")


def test_newer_signed_update_returns_download_metadata(
    configured_update_checker, monkeypatch
):
    _set_manifest(
        monkeypatch,
        {
            "version": "v2.1.0",
            "windows_app_url": "https://x/update.zip",
            "windows_app_size": "123",
            "windows_app_sha256": "ABCDEF",
            "notes": " New release ",
        },
    )
    monkeypatch.setattr(
        update_checker, "_verify_manifest_signature_required", lambda _payload: True
    )
    result = update_checker.check_for_update("1.9.0")
    assert result.update_available
    assert result.latest_version == "v2.1.0"
    assert result.download_url == "https://x/update.zip"
    assert result.download_size_bytes == 123
    assert result.download_sha256 == "abcdef"
    assert result.package_kind == "zip"
    assert result.release_notes == "New release"


def test_signature_check_requires_configured_url(monkeypatch):
    monkeypatch.setattr(update_checker, "_MANIFEST_SIGNATURE_URL", "")
    assert not update_checker._verify_manifest_signature_required(b"manifest")


@pytest.mark.parametrize("code", [404, 500])
def test_signature_check_handles_http_errors(monkeypatch, code):
    monkeypatch.setattr(update_checker, "_MANIFEST_SIGNATURE_URL", "https://x/sig")
    error = urllib.error.HTTPError("url", code, "failure", {}, None)
    monkeypatch.setattr(
        update_checker,
        "_fetch_url_bytes",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(error),
    )
    assert not update_checker._verify_manifest_signature_required(b"manifest")


def test_signature_check_handles_verification_failure(monkeypatch):
    monkeypatch.setattr(update_checker, "_MANIFEST_SIGNATURE_URL", "https://x/sig")
    monkeypatch.setattr(update_checker, "_fetch_url_bytes", lambda *_args, **_kwargs: b"sig")
    monkeypatch.setattr(
        update_checker,
        "verify_update_manifest_signature",
        lambda *_args: (_ for _ in ()).throw(SignatureVerificationError("bad sig")),
    )
    assert not update_checker._verify_manifest_signature_required(b"manifest")


def test_signature_check_accepts_valid_signature(monkeypatch):
    monkeypatch.setattr(update_checker, "_MANIFEST_SIGNATURE_URL", "https://x/sig")
    monkeypatch.setattr(update_checker, "_fetch_url_bytes", lambda *_args, **_kwargs: b"sig")
    verified = []
    monkeypatch.setattr(
        update_checker,
        "verify_update_manifest_signature",
        lambda manifest, signature: verified.append((manifest, signature)),
    )
    assert update_checker._verify_manifest_signature_required(b"manifest")
    assert verified == [(b"manifest", b"sig")]
