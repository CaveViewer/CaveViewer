"""Contracts for the repository's CodeQL default-setup configuration."""

import re
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
CODEQL_CONFIG = REPOSITORY_ROOT / ".github" / "codeql" / "codeql-config.yml"
EXPECTED_LOCAL_BENCHMARK_EXCLUSIONS = (
    "benchmarks/**",
    "scripts/benchmark/**",
    "src/caveviewer/benchmark.py",
    "src/caveviewer/benchmarking/**",
    "tests/unit/benchmarking/**",
)


def test_codeql_excludes_only_the_local_benchmark_surface():
    config = CODEQL_CONFIG.read_text(encoding="utf-8")
    exclusions = tuple(re.findall(r'^  - "([^"]+)"$', config, flags=re.MULTILINE))

    assert exclusions == EXPECTED_LOCAL_BENCHMARK_EXCLUSIONS
    assert "paths-ignore:" in config
    assert "\npaths:" not in config

    for exclusion in exclusions:
        repository_path = exclusion.removesuffix("/**")
        assert (REPOSITORY_ROOT / repository_path).exists()


def test_codeql_exclusions_do_not_hide_broad_repository_surfaces():
    config = CODEQL_CONFIG.read_text(encoding="utf-8")

    for broad_exclusion in (
        '  - "**"',
        '  - ".github/**"',
        '  - "src/**"',
        '  - "tests/**"',
        '  - "website/**"',
    ):
        assert broad_exclusion not in config
