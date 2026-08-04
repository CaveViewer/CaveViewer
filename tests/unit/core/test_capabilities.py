"""Test immutable, GUI-independent capability snapshot values."""

from __future__ import annotations

import pytest

from caveviewer.core.capabilities import (
    CapabilityResult,
    CapabilitySource,
    CapabilityStatus,
    DirectorySelectionRoute,
    DirectorySelectionTarget,
)


def test_capability_result_copies_and_freezes_scalar_evidence():
    evidence = {"install_channel": "linux_app", "attempt": 1}

    result = CapabilityResult.available(
        "signed_manifest",
        reason_code="automatic_update_target_available",
        source=CapabilitySource.DETECTED,
        evidence=evidence,
    )
    evidence["attempt"] = 2

    assert result.status is CapabilityStatus.AVAILABLE
    assert result.value == "signed_manifest"
    assert dict(result.evidence) == {
        "install_channel": "linux_app",
        "attempt": 1,
    }
    with pytest.raises(TypeError):
        result.evidence["new"] = "value"  # type: ignore[index]


@pytest.mark.parametrize("reason_code", ("", "   "))
def test_capability_result_requires_a_stable_reason_code(reason_code):
    with pytest.raises(ValueError, match="reason_code"):
        CapabilityResult.unavailable(reason_code=reason_code)


def test_capability_result_rejects_mutable_evidence_values():
    with pytest.raises(TypeError, match="scalar diagnostics"):
        CapabilityResult.unknown(
            reason_code="probe_failed",
            evidence={"details": ["mutable"]},  # type: ignore[dict-item]
        )


def test_directory_selection_target_validates_known_distinct_routes():
    target = DirectorySelectionTarget(
        primary_route=DirectorySelectionRoute.PORTAL,
        fallback_route=DirectorySelectionRoute.TK,
    )

    assert target.route_key == "portal_then_tk"
    with pytest.raises(ValueError, match="must differ"):
        DirectorySelectionTarget(
            primary_route=DirectorySelectionRoute.TK,
            fallback_route=DirectorySelectionRoute.TK,
        )
    with pytest.raises(TypeError, match="known route"):
        DirectorySelectionTarget(primary_route="tk")  # type: ignore[arg-type]
