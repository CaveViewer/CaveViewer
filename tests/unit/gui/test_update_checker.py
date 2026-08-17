"""Validate typed signed update manifests, downloads, and error handling."""

from __future__ import annotations

import json
import logging
import ssl
import urllib.error

import pytest

from caveviewer.gui import update_checker
from caveviewer.gui.platform.probes.updates import (
    UpdateManifestSchema,
    UpdateTarget,
)
from caveviewer.gui.update_signature import SignatureVerificationError


class FakeTlsTrustAdapter:
    def __init__(self):
        self.contexts = []

    def augment_ssl_context(self, context):
        self.contexts.append(context)


@pytest.fixture
def update_target():
    return UpdateTarget(
        install_channel="windows_app",
        manifest_url="https://updates.example/stable.json",
        manifest_signature_url="https://updates.example/stable.json.sig",
        user_agent="CaveViewer-Target-Test",
        manifest_schema=UpdateManifestSchema(
            download_url_keys=("windows_app_url", "download_url"),
            download_size_keys=("windows_app_size", "download_size_bytes"),
            download_sha256_keys=("windows_app_sha256", "sha256"),
            allowed_package_kinds=frozenset({"zip", "msi", "exe"}),
            missing_download_url_message="Missing update download URL.",
        ),
    )


@pytest.fixture
def tls_trust_adapter():
    return FakeTlsTrustAdapter()


def _set_manifest(monkeypatch, data):
    payload = json.dumps(data).encode("utf-8")
    calls = []

    def fetch(url, *, headers, timeout, tls_trust_adapter):
        calls.append((url, headers, timeout, tls_trust_adapter))
        return payload

    monkeypatch.setattr(update_checker, "_fetch_url_bytes_for_adapter", fetch)
    return payload, calls


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("1.2.3", (1, 2, 3)),
        ("v2.10", (2, 10)),
        ("bad", (0,)),
        ("1.beta", (0,)),
        ("1.0.64-rc1", (0,)),
    ],
)
def test_parse_version(text, expected):
    assert update_checker._parse_version(text) == expected


def test_manifest_value_helpers_use_first_valid_alias():
    data = {
        "old": " ",
        "new": " value ",
        "bad_size": "42",
        "size": 42,
        "bad_sha": "not-a-digest",
        "sha": "A" * 64,
    }

    assert update_checker._first_non_empty_str(data, ("old", "new")) == "value"
    assert update_checker._first_positive_int(data, ("bad_size", "size")) == 42
    assert update_checker._first_valid_sha256(data, ("bad_sha", "sha")) == "a" * 64


@pytest.mark.parametrize(
    ("version", "expected"),
    [
        ("1.0.0", True),
        ("v1.0.0", True),
        ("1", False),
        ("1.0.0-rc1", False),
        ("not-a-version", False),
    ],
)
def test_release_version_validation(version, expected):
    assert update_checker._is_release_version(version) is expected


def test_fetch_url_bytes_uses_headers_timeout_and_tls_context(monkeypatch):
    opened = {}
    ssl_context = object()
    tls_trust_adapter = FakeTlsTrustAdapter()

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def read(self):
            return b"manifest"

    def urlopen(request, *, timeout, context):
        opened.update(request=request, timeout=timeout, context=context)
        return FakeResponse()

    monkeypatch.setattr(
        update_checker,
        "make_ssl_context",
        lambda *, tls_trust_adapter: ssl_context,
    )
    monkeypatch.setattr(update_checker.urllib.request, "urlopen", urlopen)

    payload = update_checker._fetch_url_bytes_for_adapter(
        "https://example.invalid/stable.json",
        headers={"Accept": "application/json"},
        timeout=17,
        tls_trust_adapter=tls_trust_adapter,
    )

    assert payload == b"manifest"
    assert opened["request"].full_url == "https://example.invalid/stable.json"
    assert opened["request"].get_header("Accept") == "application/json"
    assert opened["timeout"] == 17
    assert opened["context"] is ssl_context


def test_download_update_target_uses_target_user_agent_and_tls_context(
    monkeypatch,
    tmp_path,
    update_target,
    tls_trust_adapter,
):
    context = object()
    context_calls = []
    opened = {}

    class FakeResponse:
        headers = {}

        def __init__(self):
            self._chunks = iter((b"payload", b""))

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def read(self, _size):
            return next(self._chunks)

    monkeypatch.setattr(
        update_checker,
        "make_ssl_context",
        lambda *, tls_trust_adapter: context_calls.append(tls_trust_adapter)
        or context,
    )
    monkeypatch.setattr(
        update_checker.urllib.request,
        "urlopen",
        lambda request, *, timeout, context: opened.update(
            request=request,
            timeout=timeout,
            context=context,
        )
        or FakeResponse(),
    )
    destination = tmp_path / "CaveViewer.zip"

    update_checker.download_update_target(
        "https://updates.example/CaveViewer.zip",
        7,
        str(destination),
        update_target=update_target,
        tls_trust_adapter=tls_trust_adapter,
    )

    assert destination.read_bytes() == b"payload"
    assert opened["request"].get_header("User-agent") == "CaveViewer-Target-Test"
    assert opened["timeout"] == 30
    assert opened["context"] is context
    assert context_calls == [tls_trust_adapter]


@pytest.mark.parametrize("code", [404, 500])
def test_update_check_handles_manifest_http_errors(
    monkeypatch,
    update_target,
    tls_trust_adapter,
    code,
):
    error = urllib.error.HTTPError("url", code, "failure", {}, None)
    monkeypatch.setattr(
        update_checker,
        "_fetch_url_bytes_for_adapter",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(error),
    )

    result = update_checker.check_for_update_target(
        "1.0.0",
        update_target=update_target,
        tls_trust_adapter=tls_trust_adapter,
    )

    assert not result.update_available
    assert str(code) in (result.error or "")


def test_update_check_handles_network_error(
    monkeypatch,
    update_target,
    tls_trust_adapter,
):
    monkeypatch.setattr(
        update_checker,
        "_fetch_url_bytes_for_adapter",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            urllib.error.URLError("offline")
        ),
    )

    result = update_checker.check_for_update_target(
        "1.0.0",
        update_target=update_target,
        tls_trust_adapter=tls_trust_adapter,
    )

    assert not result.update_available
    assert "Couldn't reach" in (result.error or "")


def test_update_check_handles_tls_context_error(
    monkeypatch,
    update_target,
    tls_trust_adapter,
):
    monkeypatch.setattr(
        update_checker,
        "make_ssl_context",
        lambda **_kwargs: (_ for _ in ()).throw(ssl.SSLError("store unavailable")),
    )

    result = update_checker.check_for_update_target(
        "1.0.0",
        update_target=update_target,
        tls_trust_adapter=tls_trust_adapter,
    )

    assert not result.update_available
    assert "Couldn't reach" in (result.error or "")


def test_update_check_handles_invalid_json(
    monkeypatch,
    update_target,
    tls_trust_adapter,
):
    monkeypatch.setattr(
        update_checker,
        "_fetch_url_bytes_for_adapter",
        lambda *_args, **_kwargs: b"{broken",
    )

    result = update_checker.check_for_update_target(
        "1.0.0",
        update_target=update_target,
        tls_trust_adapter=tls_trust_adapter,
    )

    assert not result.update_available
    assert "unexpected update manifest format" in (result.error or "")


def test_update_check_rejects_non_object_manifest(
    monkeypatch,
    update_target,
    tls_trust_adapter,
):
    _set_manifest(monkeypatch, ["not", "a", "manifest"])

    result = update_checker.check_for_update_target(
        "1.0.0",
        update_target=update_target,
        tls_trust_adapter=tls_trust_adapter,
    )

    assert not result.update_available
    assert "JSON object" in (result.error or "")


def test_update_check_rejects_wrong_payload_type(
    monkeypatch,
    update_target,
    tls_trust_adapter,
):
    _set_manifest(
        monkeypatch,
        {"latest_version": "2.0.0", "windows_app_url": "https://x/update.dmg"},
    )

    result = update_checker.check_for_update_target(
        "1.0.0",
        update_target=update_target,
        tls_trust_adapter=tls_trust_adapter,
    )

    assert not result.update_available
    assert "not valid" in (result.error or "")


def test_update_check_rejects_missing_version(
    monkeypatch,
    update_target,
    tls_trust_adapter,
):
    _set_manifest(monkeypatch, {"download_url": "https://x/update.zip"})

    result = update_checker.check_for_update_target(
        "1.0.0",
        update_target=update_target,
        tls_trust_adapter=tls_trust_adapter,
    )

    assert not result.update_available
    assert "latest_version" in (result.error or "")


def test_update_check_rejects_invalid_version(
    monkeypatch,
    update_target,
    tls_trust_adapter,
):
    _set_manifest(
        monkeypatch,
        {"latest_version": "2.0.0-rc1", "windows_app_url": "https://x/update.zip"},
    )

    result = update_checker.check_for_update_target(
        "1.0.0",
        update_target=update_target,
        tls_trust_adapter=tls_trust_adapter,
    )

    assert not result.update_available
    assert "invalid latest_version" in (result.error or "")


def test_update_check_rejects_missing_download_url(
    monkeypatch,
    update_target,
    tls_trust_adapter,
):
    _set_manifest(monkeypatch, {"latest_version": "2.0.0"})

    result = update_checker.check_for_update_target(
        "1.0.0",
        update_target=update_target,
        tls_trust_adapter=tls_trust_adapter,
    )

    assert not result.update_available
    assert "Missing update download URL" in (result.error or "")


def test_current_version_does_not_require_signature(
    monkeypatch,
    caplog,
    update_target,
    tls_trust_adapter,
):
    _, calls = _set_manifest(
        monkeypatch,
        {
            "latest_version": "1.0.0",
            "windows_app_url": "https://x/update.zip",
            "release_notes": "Already current",
        },
    )
    monkeypatch.setattr(
        update_checker,
        "_verify_manifest_signature_required_with_target",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("must not verify current release")
        ),
    )

    with caplog.at_level(logging.INFO, logger="caveviewer"):
        result = update_checker.check_for_update_target(
            "1.0.0",
            update_target=update_target,
            tls_trust_adapter=tls_trust_adapter,
        )

    assert not result.update_available
    assert result.latest_version == "1.0.0"
    assert result.release_notes == "Already current"
    assert calls == [
        (
            update_target.manifest_url,
            {"Accept": "application/json", "User-Agent": "CaveViewer-Target-Test"},
            8,
            tls_trust_adapter,
        )
    ]
    assert any(
        record.getMessage().startswith("No update available:")
        for record in caplog.records
    )


def test_newer_update_requires_valid_signature(
    monkeypatch,
    update_target,
    tls_trust_adapter,
):
    _set_manifest(
        monkeypatch,
        {
            "latest_version": "2.0.0",
            "windows_app_url": "https://x/update.zip",
            "windows_app_size": 123,
            "windows_app_sha256": "a" * 64,
        },
    )
    monkeypatch.setattr(
        update_checker,
        "_verify_manifest_signature_required_with_target",
        lambda *_args, **_kwargs: False,
    )

    result = update_checker.check_for_update_target(
        "1.0.0",
        update_target=update_target,
        tls_trust_adapter=tls_trust_adapter,
    )

    assert not result.update_available
    assert "signature" in (result.error or "")


def test_newer_signed_update_returns_download_metadata(
    monkeypatch,
    update_target,
    tls_trust_adapter,
):
    _set_manifest(
        monkeypatch,
        {
            "version": "v2.1.0",
            "windows_app_url": "https://x/update.zip",
            "windows_app_size": 123,
            "windows_app_sha256": "A" * 64,
            "notes": " New release ",
        },
    )
    monkeypatch.setattr(
        update_checker,
        "_verify_manifest_signature_required_with_target",
        lambda *_args, **_kwargs: True,
    )

    result = update_checker.check_for_update_target(
        "1.9.0",
        update_target=update_target,
        tls_trust_adapter=tls_trust_adapter,
    )

    assert result.update_available
    assert result.latest_version == "v2.1.0"
    assert result.download_url == "https://x/update.zip"
    assert result.download_size_bytes == 123
    assert result.download_sha256 == "a" * 64
    assert result.package_kind == "zip"
    assert result.release_notes == "New release"


@pytest.mark.parametrize(
    ("manifest_fields", "expected_error"),
    [
        ({"windows_app_url": "http://x/update.zip"}, "must use HTTPS"),
        ({"windows_app_url": "https://[invalid/update.zip"}, "must use HTTPS"),
        ({"windows_app_size": 0}, "positive integer"),
        ({"windows_app_size": -1}, "positive integer"),
        ({"windows_app_size": "123"}, "positive integer"),
        ({"windows_app_sha256": ""}, "64-character hexadecimal"),
        ({"windows_app_sha256": "a" * 63}, "64-character hexadecimal"),
    ],
)
def test_newer_update_rejects_incomplete_artifact_contract_before_signature(
    monkeypatch,
    update_target,
    tls_trust_adapter,
    manifest_fields,
    expected_error,
):
    manifest = {
        "latest_version": "2.0.0",
        "windows_app_url": "https://x/update.zip",
        "windows_app_size": 123,
        "windows_app_sha256": "a" * 64,
    }
    manifest.update(manifest_fields)
    _set_manifest(monkeypatch, manifest)
    monkeypatch.setattr(
        update_checker,
        "_verify_manifest_signature_required_with_target",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("must not verify an invalid artifact contract")
        ),
    )

    result = update_checker.check_for_update_target(
        "1.0.0",
        update_target=update_target,
        tls_trust_adapter=tls_trust_adapter,
    )

    assert not result.update_available
    assert expected_error in (result.error or "")


@pytest.mark.parametrize("code", [404, 500])
def test_signature_check_handles_http_errors(update_target, code):
    error = urllib.error.HTTPError("url", code, "failure", {}, None)

    assert not update_checker._verify_manifest_signature_required_with_target(
        b"manifest",
        update_target=update_target,
        fetch_url_bytes=lambda *_args, **_kwargs: (_ for _ in ()).throw(error),
    )


def test_signature_check_handles_network_error(update_target):
    assert not update_checker._verify_manifest_signature_required_with_target(
        b"manifest",
        update_target=update_target,
        fetch_url_bytes=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            urllib.error.URLError("offline")
        ),
    )


def test_signature_check_handles_verification_failure(monkeypatch, update_target):
    monkeypatch.setattr(
        update_checker,
        "verify_update_manifest_signature",
        lambda *_args: (_ for _ in ()).throw(SignatureVerificationError("bad sig")),
    )

    assert not update_checker._verify_manifest_signature_required_with_target(
        b"manifest",
        update_target=update_target,
        fetch_url_bytes=lambda *_args, **_kwargs: b"signature",
    )


def test_signature_check_accepts_valid_signature(monkeypatch, update_target):
    verified = []
    fetched = []
    monkeypatch.setattr(
        update_checker,
        "verify_update_manifest_signature",
        lambda manifest, signature: verified.append((manifest, signature)),
    )

    assert update_checker._verify_manifest_signature_required_with_target(
        b"manifest",
        update_target=update_target,
        fetch_url_bytes=lambda url, headers, timeout: fetched.append(
            (url, headers, timeout)
        )
        or b"signature",
    )
    assert verified == [(b"manifest", b"signature")]
    assert fetched == [
        (
            update_target.manifest_signature_url,
            {
                "Accept": "text/plain, application/octet-stream",
                "User-Agent": "CaveViewer-Target-Test",
            },
            8,
        )
    ]
