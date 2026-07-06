from __future__ import annotations

import base64
import binascii
import os
import sys
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from cryptography.hazmat.primitives import serialization

from core.logging_utils import get_logger


_PUBLIC_KEY_RELATIVE_PATH = ("security", "release_signing_public_key.pem")
_LOG = get_logger("UpdateSignature")


class SignatureVerificationError(RuntimeError):
    """Raised when an update manifest signature cannot be trusted."""


def default_manifest_signature_url(manifest_url: str) -> str:
    return f"{manifest_url}.sig"


def verify_update_manifest_signature(manifest_bytes: bytes, signature_bytes: bytes) -> None:
    _LOG.info(
        "Verifying update manifest signature: manifest_bytes=%d, signature_bytes=%d",
        len(manifest_bytes),
        len(signature_bytes),
    )
    public_key = _load_release_public_key()
    signature = _decode_signature(signature_bytes)
    try:
        public_key.verify(signature, manifest_bytes)
    except InvalidSignature as exc:
        _LOG.warning("Update manifest signature verification failed: invalid signature.")
        raise SignatureVerificationError("Update manifest signature is invalid.") from exc
    _LOG.info("Update manifest signature verification passed.")


def _decode_signature(signature_bytes: bytes) -> bytes:
    text = signature_bytes.strip()
    if not text:
        _LOG.warning("Update manifest signature verification failed: signature is empty.")
        raise SignatureVerificationError("Update manifest signature is empty.")
    try:
        decoded = base64.b64decode(text, validate=True)
    except (binascii.Error, ValueError) as exc:
        _LOG.warning("Update manifest signature verification failed: signature is not valid base64.")
        raise SignatureVerificationError(
            "Update manifest signature is not valid base64."
        ) from exc
    _LOG.info("Decoded update manifest signature: decoded_bytes=%d", len(decoded))
    return decoded


def _load_release_public_key():
    key_path = _resolve_release_public_key_path()
    if key_path is None:
        searched = ", ".join(str(path) for path in _candidate_public_key_paths())
        _LOG.warning("Release signing public key not found. Searched: %s", searched)
        raise SignatureVerificationError(
            f"Release signing public key not found. Searched: {searched}"
        )

    try:
        _LOG.info("Loading release signing public key: %s", key_path)
        key_bytes = key_path.read_bytes()
        public_key = serialization.load_pem_public_key(key_bytes)
    except Exception as exc:
        _LOG.warning("Could not load release signing public key at %s: %s", key_path, exc)
        raise SignatureVerificationError(
            f"Could not load release signing public key at {key_path}: {exc}"
        ) from exc

    if not isinstance(public_key, Ed25519PublicKey):
        _LOG.warning("Release signing public key is not Ed25519: %s", key_path)
        raise SignatureVerificationError(
            f"Release signing public key at {key_path} is not an Ed25519 public key."
        )
    _LOG.info("Loaded Ed25519 release signing public key: %s", key_path)
    return public_key


def _resolve_release_public_key_path() -> Path | None:
    for path in _candidate_public_key_paths():
        if path.is_file():
            return path
    return None


def _candidate_public_key_paths() -> list[Path]:
    rel_path = Path(*_PUBLIC_KEY_RELATIVE_PATH)
    candidates: list[Path] = []

    if hasattr(sys, "frozen") and hasattr(sys, "_MEIPASS"):
        candidates.append(Path(sys._MEIPASS) / rel_path)  # type: ignore[attr-defined]

    module_root = Path(__file__).resolve().parent.parent
    candidates.append(module_root / rel_path)

    if getattr(sys, "executable", ""):
        exe_dir = Path(sys.executable).resolve().parent
        candidates.append(exe_dir / rel_path)
        candidates.append(exe_dir.parent / rel_path)

    if sys.argv and sys.argv[0]:
        candidates.append(Path(sys.argv[0]).resolve().parent / rel_path)

    cwd = Path(os.getcwd()).resolve()
    candidates.append(cwd / rel_path)

    return candidates
