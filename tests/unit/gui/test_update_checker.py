"""Validate typed signed update manifests, downloads, and error handling."""

from __future__ import annotations

import json
import logging
import ssl
import urllib.error
from dataclasses import replace

import pytest

from caveviewer.gui import update_checker
from caveviewer.gui.platform.probes.updates import (
    UpdateManifestSchema,
    UpdateTarget,
    select_update_profile,
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
        manifest_channel="stable",
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
    monkeypatch.setattr(
        update_checker,
        "_probe_update_artifact_for_adapter",
        lambda *_args, **_kwargs: None,
    )
    return payload, calls


def _assert_failed(result):
    assert isinstance(result, update_checker.UpdateCheckFailed)
    return result


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


def test_manifest_parser_returns_a_complete_artifact_for_a_newer_update(
    update_target,
):
    parsed = update_checker._parse_update_manifest(
        "1.0.0",
        {
            "latest_version": "2.0.0",
            "windows_app_url": "https://updates.example/CaveViewer.zip",
            "windows_app_size": 123,
            "windows_app_sha256": "A" * 64,
        },
        update_target=update_target,
        package_kind_for_url=lambda _url: "zip",
    )

    assert isinstance(parsed, update_checker.UpdateArtifact)
    assert parsed.version == "2.0.0"
    assert parsed.download_url == "https://updates.example/CaveViewer.zip"
    assert parsed.size_bytes == 123
    assert parsed.sha256 == "a" * 64
    assert parsed.package_kind == "zip"


def test_manifest_release_channel_must_match_the_target_channel(update_target):
    manifest = {
        "latest_version": "2.0.0",
        "release_channel": "preview",
        "windows_app_url": "https://updates.example/CaveViewer.zip",
        "windows_app_size": 123,
        "windows_app_sha256": "A" * 64,
    }

    rejected = update_checker._parse_update_manifest(
        "1.0.0",
        manifest,
        update_target=update_target,
        package_kind_for_url=lambda _url: "zip",
    )
    manifest["release_channel"] = "stable"
    accepted = update_checker._parse_update_manifest(
        "1.0.0",
        manifest,
        update_target=update_target,
        package_kind_for_url=lambda _url: "zip",
    )

    assert isinstance(rejected, update_checker.UpdateCheckFailed)
    assert "does not match" in rejected.error
    assert isinstance(accepted, update_checker.UpdateArtifact)


def test_manifest_without_release_channel_is_accepted_during_the_transition(
    caplog, update_target
):
    with caplog.at_level(logging.WARNING, logger="caveviewer"):
        parsed = update_checker._parse_update_manifest(
            "1.0.0",
            {
                "latest_version": "2.0.0",
                "windows_app_url": "https://updates.example/CaveViewer.zip",
                "windows_app_size": 123,
                "windows_app_sha256": "A" * 64,
            },
            update_target=update_target,
            package_kind_for_url=lambda _url: "zip",
        )

    assert isinstance(parsed, update_checker.UpdateArtifact)
    assert any("legacy manifest" in record.getMessage() for record in caplog.records)


def _windows_exe_target() -> UpdateTarget:
    profile = select_update_profile(platform_name="win32", machine="AMD64")
    return UpdateTarget(
        install_channel=profile.install_channel,
        manifest_channel="stable",
        manifest_url="https://updates.example/windows/stable.json",
        manifest_signature_url="https://updates.example/windows/stable.json.sig",
        user_agent="CaveViewer-Windows-Installer-Test",
        manifest_schema=profile.manifest_schema,
    )


def test_windows_exe_manifest_requires_its_installer_channel_and_signer_subject():
    target = _windows_exe_target()
    manifest = {
        "latest_version": "2.0.0",
        "download_url_windows_exe": "https://updates.example/CaveViewer-2.0.0-windows.exe",
        "download_size_bytes_windows_exe": 123,
        "sha256_windows_exe": "A" * 64,
        "install_channel": "windows_installer",
        "authenticode_certificate_subject": "CN=CaveViewer Update Publisher",
    }

    parsed = update_checker._parse_update_manifest(
        "1.0.0",
        manifest,
        update_target=target,
        package_kind_for_url=lambda _url: "exe",
    )

    assert isinstance(parsed, update_checker.UpdateArtifact)
    assert parsed.package_kind == "exe"
    assert parsed.authenticode_certificate_subject == "CN=CaveViewer Update Publisher"
    # Older signed manifests did not have an explicit status field.
    assert parsed.authenticode_status == "verified"

    manifest.pop("install_channel")
    rejected_channel = update_checker._parse_update_manifest(
        "1.0.0",
        manifest,
        update_target=target,
        package_kind_for_url=lambda _url: "exe",
    )
    assert isinstance(rejected_channel, update_checker.UpdateCheckFailed)
    assert "installer channel" in rejected_channel.error

    manifest["install_channel"] = "windows_installer"
    manifest.pop("authenticode_certificate_subject")
    rejected_subject = update_checker._parse_update_manifest(
        "1.0.0",
        manifest,
        update_target=target,
        package_kind_for_url=lambda _url: "exe",
    )
    assert isinstance(rejected_subject, update_checker.UpdateCheckFailed)
    assert "Authenticode certificate subject" in rejected_subject.error


def test_unsigned_community_windows_exe_manifest_is_explicit_and_has_no_subject():
    target = _windows_exe_target()
    manifest = {
        "latest_version": "2.0.0",
        "download_url_windows_exe": "https://updates.example/CaveViewer-2.0.0-windows.exe",
        "download_size_bytes_windows_exe": 123,
        "sha256_windows_exe": "A" * 64,
        "install_channel": "windows_installer",
        "authenticode_status": "unsigned-community",
    }

    parsed = update_checker._parse_update_manifest(
        "1.0.0",
        manifest,
        update_target=target,
        package_kind_for_url=lambda _url: "exe",
    )

    assert isinstance(parsed, update_checker.UpdateArtifact)
    assert parsed.authenticode_status == "unsigned-community"
    assert parsed.authenticode_certificate_subject is None

    manifest["authenticode_certificate_subject"] = "CN=Unexpected Publisher"
    rejected_subject = update_checker._parse_update_manifest(
        "1.0.0",
        manifest,
        update_target=target,
        package_kind_for_url=lambda _url: "exe",
    )
    assert isinstance(rejected_subject, update_checker.UpdateCheckFailed)
    assert "must not declare" in rejected_subject.error

    manifest.pop("authenticode_certificate_subject")
    manifest["authenticode_status"] = "unsigned-test-only"
    rejected_status = update_checker._parse_update_manifest(
        "1.0.0",
        manifest,
        update_target=target,
        package_kind_for_url=lambda _url: "exe",
    )
    assert isinstance(rejected_status, update_checker.UpdateCheckFailed)
    assert "unsupported Windows installer Authenticode status" in rejected_status.error


def test_windows_zip_manifest_remains_a_manual_migration_package_without_signer_data():
    parsed = update_checker._parse_update_manifest(
        "1.0.0",
        {
            "latest_version": "2.0.0",
            "download_url_windows_zip": "https://updates.example/CaveViewer-2.0.0-windows.zip",
            "download_size_bytes_windows_zip": 123,
            "sha256_windows_zip": "A" * 64,
        },
        update_target=_windows_exe_target(),
        package_kind_for_url=lambda _url: "zip",
    )

    assert isinstance(parsed, update_checker.UpdateArtifact)
    assert parsed.package_kind == "zip"
    assert parsed.authenticode_certificate_subject is None


def test_manifest_parser_returns_up_to_date_without_an_artifact(update_target):
    parsed = update_checker._parse_update_manifest(
        "1.0.0",
        {
            "latest_version": "1.0.0",
            "windows_app_url": "https://updates.example/CaveViewer.zip",
        },
        update_target=update_target,
        package_kind_for_url=lambda _url: "zip",
    )

    assert isinstance(parsed, update_checker.UpdateNotAvailable)
    assert parsed.current_version == "1.0.0"
    assert parsed.latest_version == "1.0.0"
    assert not hasattr(parsed, "artifact")


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


def test_artifact_probe_uses_head_with_headers_timeout_and_tls_context(monkeypatch):
    opened = {}
    ssl_context = object()
    tls_trust_adapter = FakeTlsTrustAdapter()

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

    def urlopen(request, *, timeout, context):
        opened.update(request=request, timeout=timeout, context=context)
        return FakeResponse()

    monkeypatch.setattr(
        update_checker,
        "make_ssl_context",
        lambda *, tls_trust_adapter: ssl_context,
    )
    monkeypatch.setattr(update_checker.urllib.request, "urlopen", urlopen)

    update_checker._probe_update_artifact_for_adapter(
        "https://example.invalid/CaveViewer.zip",
        headers={"User-Agent": "CaveViewer-Probe-Test"},
        timeout=17,
        tls_trust_adapter=tls_trust_adapter,
    )

    assert opened["request"].get_method() == "HEAD"
    assert opened["request"].full_url == "https://example.invalid/CaveViewer.zip"
    assert opened["request"].get_header("User-agent") == "CaveViewer-Probe-Test"
    assert opened["timeout"] == 17
    assert opened["context"] is ssl_context


@pytest.mark.parametrize("head_status", [405, 501])
def test_artifact_probe_falls_back_to_a_one_byte_get(monkeypatch, head_status):
    requests = []

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def read(self, size):
            assert size == 1
            return b"x"

    def urlopen(request, *, timeout, context):
        requests.append(request)
        if request.get_method() == "HEAD":
            raise urllib.error.HTTPError(
                request.full_url,
                head_status,
                "HEAD unsupported",
                {},
                None,
            )
        return FakeResponse()

    monkeypatch.setattr(update_checker, "make_ssl_context", lambda **_kwargs: object())
    monkeypatch.setattr(update_checker.urllib.request, "urlopen", urlopen)

    update_checker._probe_update_artifact_for_adapter(
        "https://example.invalid/CaveViewer.zip",
        headers={"Accept": "application/octet-stream"},
        timeout=8,
        tls_trust_adapter=FakeTlsTrustAdapter(),
    )

    assert [request.get_method() for request in requests] == ["HEAD", "GET"]
    assert requests[1].get_header("Range") == "bytes=0-0"


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

    failure = _assert_failed(result)
    assert str(code) in failure.error


def test_missing_preview_manifest_means_no_preview_is_available(
    monkeypatch,
    caplog,
    update_target,
    tls_trust_adapter,
):
    preview_target = replace(
        update_target,
        manifest_channel="preview",
        manifest_url="https://updates.example/preview.json",
        manifest_signature_url="https://updates.example/preview.json.sig",
    )
    error = urllib.error.HTTPError(
        preview_target.manifest_url,
        404,
        "not found",
        {},
        None,
    )
    monkeypatch.setattr(
        update_checker,
        "_fetch_url_bytes_for_adapter",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(error),
    )

    with caplog.at_level(logging.INFO, logger="caveviewer"):
        result = update_checker.check_for_update_target(
            "1.0.0",
            update_target=preview_target,
            tls_trust_adapter=tls_trust_adapter,
        )

    assert isinstance(result, update_checker.UpdateNotAvailable)
    assert result.current_version == "1.0.0"
    assert result.latest_version is None
    assert any(
        "No preview update manifest is published" in record.getMessage()
        for record in caplog.records
    )


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

    failure = _assert_failed(result)
    assert "Couldn't reach" in failure.error


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

    failure = _assert_failed(result)
    assert "Couldn't reach" in failure.error


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

    failure = _assert_failed(result)
    assert "unexpected update manifest format" in failure.error


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

    failure = _assert_failed(result)
    assert "JSON object" in failure.error


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

    failure = _assert_failed(result)
    assert "not valid" in failure.error


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

    failure = _assert_failed(result)
    assert "latest_version" in failure.error


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

    failure = _assert_failed(result)
    assert "invalid latest_version" in failure.error


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

    failure = _assert_failed(result)
    assert "Missing update download URL" in failure.error


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

    assert isinstance(result, update_checker.UpdateNotAvailable)
    assert result.latest_version == "1.0.0"
    assert not hasattr(result, "artifact")
    assert not hasattr(result, "release_notes")
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
    monkeypatch.setattr(
        update_checker,
        "_probe_update_artifact_for_adapter",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("must not probe an artifact from an unverified manifest")
        ),
    )

    result = update_checker.check_for_update_target(
        "1.0.0",
        update_target=update_target,
        tls_trust_adapter=tls_trust_adapter,
    )

    failure = _assert_failed(result)
    assert "signature" in failure.error


def test_newer_signed_update_returns_validated_artifact(
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

    assert isinstance(result, update_checker.UpdateAvailable)
    assert result.current_version == "1.9.0"
    assert result.artifact.version == "v2.1.0"
    assert result.artifact.download_url == "https://x/update.zip"
    assert result.artifact.size_bytes == 123
    assert result.artifact.sha256 == "a" * 64
    assert result.artifact.package_kind == "zip"
    assert not hasattr(result, "release_notes")


def test_newer_signed_update_probes_the_artifact_before_returning_it(
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
        lambda *_args, **_kwargs: True,
    )
    probes = []
    monkeypatch.setattr(
        update_checker,
        "_probe_update_artifact_for_adapter",
        lambda url, *, headers, timeout, tls_trust_adapter: probes.append(
            (url, headers, timeout, tls_trust_adapter)
        ),
    )

    result = update_checker.check_for_update_target(
        "1.0.0",
        update_target=update_target,
        tls_trust_adapter=tls_trust_adapter,
    )

    assert isinstance(result, update_checker.UpdateAvailable)
    assert probes == [
        (
            "https://x/update.zip",
            {
                "Accept": "application/octet-stream",
                "User-Agent": "CaveViewer-Target-Test",
            },
            8,
            tls_trust_adapter,
        )
    ]


@pytest.mark.parametrize("status", [404, 410])
def test_unavailable_signed_artifact_is_not_offered(
    monkeypatch,
    update_target,
    tls_trust_adapter,
    status,
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
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(
        update_checker,
        "_probe_update_artifact_for_adapter",
        lambda url, **_kwargs: (_ for _ in ()).throw(
            urllib.error.HTTPError(url, status, "unavailable", {}, None)
        ),
    )

    result = update_checker.check_for_update_target(
        "1.0.0",
        update_target=update_target,
        tls_trust_adapter=tls_trust_adapter,
    )

    assert isinstance(result, update_checker.UpdateNotAvailable)
    assert result.latest_version == "2.0.0"


@pytest.mark.parametrize(
    "error",
    [
        urllib.error.HTTPError("https://x/update.zip", 500, "failure", {}, None),
        urllib.error.URLError("offline"),
        OSError("TLS unavailable"),
    ],
)
def test_artifact_probe_failure_suppresses_the_update(
    monkeypatch,
    update_target,
    tls_trust_adapter,
    error,
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
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(
        update_checker,
        "_probe_update_artifact_for_adapter",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(error),
    )

    result = update_checker.check_for_update_target(
        "1.0.0",
        update_target=update_target,
        tls_trust_adapter=tls_trust_adapter,
    )

    _assert_failed(result)


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

    failure = _assert_failed(result)
    assert expected_error in failure.error


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
