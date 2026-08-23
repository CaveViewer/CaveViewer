"""Tests for release-version publication ordering."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPOSITORY_ROOT / "scripts" / "common" / "validate_release_version.py"


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "validate_release_version", SCRIPT_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    ("candidate", "published", "expected"),
    [
        ("1.0.100", ["v1.0.99", "preview"], "new"),
        ("1.1.0", ["v1.0.100", "v1.0.99"], "new"),
        ("2.0.0", ["v1.99.999"], "new"),
        ("1.0.99", ["v1.0.99", "v1.0.98"], "resume"),
        ("1.2.0", ["v1.2"], "resume"),
        ("1.0.0", [], "new"),
    ],
)
def test_classify_release_version(candidate, published, expected):
    module = _load_module()

    assert module.classify_release_version(candidate, published) == expected


@pytest.mark.parametrize("candidate", ("v1.2.3", "1.2", "1.2.03", "1.2.3.4"))
def test_classify_release_version_requires_canonical_three_components(candidate):
    module = _load_module()

    with pytest.raises(ValueError, match="canonical|two or three components"):
        module.classify_release_version(candidate, ["v1.0.0"])


def test_classify_release_version_rejects_older_candidate():
    module = _load_module()

    with pytest.raises(ValueError, match="older than published version 1.1.0"):
        module.classify_release_version("1.0.100", ["v1.1.0"])
