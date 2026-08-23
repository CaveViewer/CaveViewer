"""Cover updater signature verification fallback behavior."""

from __future__ import annotations

import builtins
import base64

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from caveviewer.gui import update_signature
from caveviewer.gui.update_signature import SignatureVerificationError


def test_signature_backend_import_failure_is_verification_error(monkeypatch):
    original_import = builtins.__import__

    def fail_cryptography_import(name, *args, **kwargs):
        if name.startswith("cryptography"):
            raise ImportError("Symbol not found: _SSL_get0_group_name")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fail_cryptography_import)

    with pytest.raises(SignatureVerificationError, match="unavailable"):
        update_signature._load_cryptography_backend()


def _write_key_pair(tmp_path, identity):
    private_key = Ed25519PrivateKey.generate()
    public_path = tmp_path / f"{identity}.pem"
    public_path.write_bytes(
        private_key.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    return private_key, public_path


def _signature(private_key, manifest=b"manifest"):
    return base64.b64encode(private_key.sign(manifest))


def test_trusted_keys_are_checked_in_primary_recovery_legacy_order(monkeypatch, tmp_path):
    private_keys = {}
    public_paths = {}
    for identity in update_signature.TRUSTED_KEY_IDENTITIES:
        private_keys[identity], public_paths[identity] = _write_key_pair(
            tmp_path,
            identity,
        )

    attempted = []
    monkeypatch.setattr(
        update_signature,
        "_candidate_public_key_paths",
        lambda identity: attempted.append(identity) or [public_paths[identity]],
    )

    verified_by = update_signature.verify_update_manifest_signature(
        b"manifest",
        _signature(private_keys["legacy"]),
    )

    assert verified_by == "legacy"
    assert attempted == ["primary", "recovery", "legacy"]


def test_primary_signature_does_not_consult_fallback_keys(monkeypatch, tmp_path):
    primary_private, primary_public = _write_key_pair(tmp_path, "primary")
    attempted = []
    monkeypatch.setattr(
        update_signature,
        "_candidate_public_key_paths",
        lambda identity: attempted.append(identity) or [primary_public],
    )

    verified_by = update_signature.verify_update_manifest_signature(
        b"manifest",
        _signature(primary_private),
    )

    assert verified_by == "primary"
    assert attempted == ["primary"]


def test_missing_and_malformed_keys_do_not_disable_recovery(monkeypatch, tmp_path):
    recovery_private, recovery_public = _write_key_pair(tmp_path, "recovery")
    malformed_public = tmp_path / "malformed.pem"
    malformed_public.write_text("not a key", encoding="utf-8")
    missing_public = tmp_path / "missing.pem"
    paths = {
        "primary": missing_public,
        "recovery": recovery_public,
        "legacy": malformed_public,
    }
    monkeypatch.setattr(
        update_signature,
        "_candidate_public_key_paths",
        lambda identity: [paths[identity]],
    )

    verified_by = update_signature.verify_update_manifest_signature(
        b"manifest",
        _signature(recovery_private),
    )

    assert verified_by == "recovery"


def test_signature_rejected_when_no_trusted_key_matches(monkeypatch, tmp_path):
    signing_private = Ed25519PrivateKey.generate()
    public_paths = {}
    for identity in update_signature.TRUSTED_KEY_IDENTITIES:
        _private, public_paths[identity] = _write_key_pair(tmp_path, identity)
    monkeypatch.setattr(
        update_signature,
        "_candidate_public_key_paths",
        lambda identity: [public_paths[identity]],
    )

    with pytest.raises(SignatureVerificationError, match="any trusted release key"):
        update_signature.verify_update_manifest_signature(
            b"manifest",
            _signature(signing_private),
        )
