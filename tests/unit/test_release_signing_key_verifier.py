"""Contracts for release signing key-pair verification."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPOSITORY_ROOT / "scripts" / "verify_release_signing_key.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("verify_release_signing_key", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _write_key_pair(directory: Path) -> tuple[Path, Path]:
    private_key = Ed25519PrivateKey.generate()
    private_path = directory / "private.pem"
    public_path = directory / "public.pem"
    private_path.write_bytes(
        private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    public_path.write_bytes(
        private_key.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    return private_path, public_path


def test_matching_ed25519_key_pair_passes(tmp_path: Path):
    module = _load_module()
    private_path, public_path = _write_key_pair(tmp_path)

    module.verify_key_pair(private_path, public_path)


def test_mismatched_ed25519_key_pair_fails(tmp_path: Path):
    module = _load_module()
    private_path, _public_path = _write_key_pair(tmp_path)
    other_directory = tmp_path / "other"
    other_directory.mkdir()
    _other_private, other_public = _write_key_pair(other_directory)

    with pytest.raises(ValueError, match="does not match"):
        module.verify_key_pair(private_path, other_public)


def test_malformed_private_key_fails_without_disclosing_key(tmp_path: Path):
    module = _load_module()
    _private_path, public_path = _write_key_pair(tmp_path)
    malformed_path = tmp_path / "malformed.pem"
    malformed_path.write_text("not-a-private-key", encoding="ascii")

    with pytest.raises(ValueError):
        module.verify_key_pair(malformed_path, public_path)
