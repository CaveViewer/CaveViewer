"""Cover RAM and GPU memory utilization target parsing."""

from __future__ import annotations

import pytest

from caveviewer.core.hardware import memory_targets


@pytest.mark.parametrize(
    ("raw_value", "expected"),
    [(None, 0.08), ("", 0.08), ("25", 0.25), ("0.5", 0.5), ("bad", 0.08)],
)
def test_ram_target_fraction_parsing(raw_value, expected):
    assert memory_targets.parse_memory_target_fraction(raw_value) == expected


@pytest.mark.parametrize(
    ("raw_value", "expected"),
    [(None, 0.70), ("", 0.70), ("25", 0.25), ("0.5", 0.5), ("bad", 0.70)],
)
def test_gpu_target_fraction_parsing(raw_value, expected):
    assert memory_targets.parse_gpu_target_fraction(raw_value) == expected
