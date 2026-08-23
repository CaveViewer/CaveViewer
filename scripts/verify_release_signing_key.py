#!/usr/bin/env python3
"""Verify that a release private key matches CaveViewer's bundled public key."""

from __future__ import annotations

import argparse
import hmac
from pathlib import Path


def verify_key_pair(private_key_path: Path, public_key_path: Path) -> None:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey,
        Ed25519PublicKey,
    )

    private_key = serialization.load_pem_private_key(
        private_key_path.read_bytes(),
        password=None,
    )
    public_key = serialization.load_pem_public_key(public_key_path.read_bytes())
    if not isinstance(private_key, Ed25519PrivateKey):
        raise ValueError("release signing private key is not Ed25519")
    if not isinstance(public_key, Ed25519PublicKey):
        raise ValueError("bundled release signing public key is not Ed25519")

    derived_public_key = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    bundled_public_key = public_key.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    if not hmac.compare_digest(derived_public_key, bundled_public_key):
        raise ValueError(
            "release signing private key does not match CaveViewer's bundled public key"
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("private_key", type=Path)
    parser.add_argument("public_key", type=Path)
    args = parser.parse_args()
    try:
        verify_key_pair(args.private_key, args.public_key)
    except (OSError, TypeError, ValueError) as exc:
        raise SystemExit(f"Error: {exc}") from exc
    print("Release signing private key matches the bundled Ed25519 public key.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
