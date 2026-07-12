from __future__ import annotations

import builtins

import pytest

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
