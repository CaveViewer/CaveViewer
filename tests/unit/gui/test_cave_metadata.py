"""Exercise offline cave metadata validation, display text, and name matching."""

from __future__ import annotations

import pytest

from caveviewer.gui.cave_metadata import (
    CaveMetadataCatalog,
    CaveMetadataStatistic,
    load_bundled_cave_metadata_catalog,
    normalized_name_variants,
)


def _cave_payload(cave_id: str, name: str, *, aliases: list[str] | None = None) -> dict:
    return {
        "id": cave_id,
        "name": name,
        "aliases": aliases or [],
        "continent": "North America",
        "country": "United States",
        "region": "Florida",
        "type": "underwater_cave",
        "statistics": [
            {
                "label": "Surveyed passage",
                "value": 8000,
                "unit": "ft",
                "qualifier": "greater_than",
            }
        ],
        "facts": ["A representative cave fact."],
        "sources": [
            {
                "title": "Reference source",
                "url": "https://example.invalid/reference",
            }
        ],
    }


def _catalog_payload(*caves: dict) -> dict:
    return {
        "schema_version": "1.0",
        "catalog": {"name": "Test catalog", "caves": list(caves)},
    }


def test_bundled_catalog_matches_known_aliases_and_accents():
    catalog = load_bundled_cave_metadata_catalog()

    devils_eye = catalog.match("Devils Eye")
    ressel = catalog.match("Emergence du Ressel")
    boh_yai = catalog.match("Boh Yai Mine I (Low Res)")

    assert devils_eye is not None
    assert devils_eye.cave.id == "us-fl-devils-spring-system"
    assert devils_eye.kind == "exact"
    assert ressel is not None
    assert ressel.cave.name == "Émergence du Ressel"
    assert boh_yai is not None
    assert boh_yai.cave.id == "th-kanchanaburi-boh-yai-mines"
    assert boh_yai.cave.library_detail == (
        "Kanchanaburi Province, Thailand · Flooded mine"
    )


def test_match_strips_technical_suffixes_and_conservative_plural_terms():
    catalog = load_bundled_cave_metadata_catalog()

    match = catalog.match("Peacock Springs Caves System (High Res)")
    filename_match = catalog.match("DevilsEyeGoldLine_resized")

    assert match is not None
    assert match.cave.id == "us-fl-peacock-springs"
    assert match.kind == "exact"
    assert match.cave.library_detail == "Florida, United States · Underwater cave"
    assert filename_match is not None
    assert filename_match.cave.id == "us-fl-devils-spring-system"


def test_match_accepts_only_a_small_fuzzy_difference_for_long_names():
    catalog = load_bundled_cave_metadata_catalog()

    match = catalog.match("Peacok Springs Cave System")

    assert match is not None
    assert match.cave.id == "us-fl-peacock-springs"
    assert match.kind == "fuzzy"
    assert match.distance == 1


def test_explicit_cave_id_wins_without_name_matching():
    catalog = load_bundled_cave_metadata_catalog()

    match = catalog.match(
        "A map title that cannot match a cave name",
        cave_metadata_id="us-fl-devils-spring-system",
    )

    assert match is not None
    assert match.cave.name == "Devil's Spring System"
    assert match.kind == "explicit"


def test_ambiguous_aliases_are_not_shown_as_a_cave_match():
    catalog = CaveMetadataCatalog.from_payload(
        _catalog_payload(
            _cave_payload("first", "First Cave", aliases=["Shared Cave"]),
            _cave_payload("second", "Second Cave", aliases=["Shared Cave"]),
        )
    )

    assert catalog.match("Shared Cave") is None


def test_catalog_rejects_unsafe_source_url():
    cave = _cave_payload("test", "Test Cave")
    cave["sources"][0]["url"] = "file:///private/source"

    with pytest.raises(ValueError, match="unsafe 'url'"):
        CaveMetadataCatalog.from_payload(_catalog_payload(cave))


def test_catalog_rejects_nonfinite_statistics():
    cave = _cave_payload("test", "Test Cave")
    cave["statistics"][0]["value"] = float("nan")

    with pytest.raises(ValueError, match="invalid 'value'"):
        CaveMetadataCatalog.from_payload(_catalog_payload(cave))


def test_display_statistic_preserves_qualifier_without_rounding_precision():
    statistic = CaveMetadataStatistic(
        label="Surveyed passage",
        value=220.33,
        unit="mi",
        qualifier="approx",
    )

    assert statistic.display_value == "About 220.33 mi"
    assert normalized_name_variants("Devil’s Eye & Ear 3D Map") == (
        "devils eye and ear 3d map",
        "devils eye and ear",
    )
