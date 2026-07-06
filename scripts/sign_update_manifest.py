#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import os
from pathlib import Path

from cryptography.hazmat.primitives import serialization


DEFAULT_PRIVATE_KEY_PATH = "~/.caveviewer/release_signing_private_key.pem"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Sign a CaveViewer update manifest with the release Ed25519 private key."
    )
    parser.add_argument("manifest", help="Path to stable.json")
    parser.add_argument(
        "--private-key",
        default=os.environ.get("CAVEVIEWER_RELEASE_SIGNING_PRIVATE_KEY", DEFAULT_PRIVATE_KEY_PATH),
        help=(
            "Path to the PEM private key. Defaults to "
            "$CAVEVIEWER_RELEASE_SIGNING_PRIVATE_KEY or "
            f"{DEFAULT_PRIVATE_KEY_PATH}."
        ),
    )
    parser.add_argument(
        "--signature",
        help="Output signature path. Defaults to <manifest>.sig.",
    )
    args = parser.parse_args()

    manifest_path = Path(args.manifest).expanduser().resolve()
    private_key_path = Path(args.private_key).expanduser().resolve()
    signature_path = (
        Path(args.signature).expanduser().resolve()
        if args.signature
        else Path(str(manifest_path) + ".sig")
    )

    if not manifest_path.is_file():
        raise SystemExit(f"Manifest not found: {manifest_path}")
    if not private_key_path.is_file():
        raise SystemExit(f"Private key not found: {private_key_path}")

    private_key = serialization.load_pem_private_key(
        private_key_path.read_bytes(),
        password=None,
    )
    signature = private_key.sign(manifest_path.read_bytes())

    signature_path.write_text(base64.b64encode(signature).decode("ascii") + "\n", encoding="ascii")
    print(f"Wrote signature: {signature_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
