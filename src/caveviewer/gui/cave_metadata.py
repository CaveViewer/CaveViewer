"""Offline cave metadata parsing and conservative Map Library name matching.

The bundled catalog is descriptive only: it never changes how a model is
opened, downloaded, cached, or validated.  This module keeps that data and its
matching policy outside Tk so the Map Library can safely show cave context for
only unambiguous rows.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Iterable
from unicodedata import normalize
from urllib.parse import urlsplit

from caveviewer.core.json_io import load_bounded_json
from caveviewer.resources import cave_metadata_catalog_path


_CATALOG_SCHEMA_VERSION = "1.0"
_MAX_CATALOG_BYTES = 128 * 1024
_TECHNICAL_NAME_SUFFIXES = (
    ("low", "resolution"),
    ("high", "resolution"),
    ("medium", "resolution"),
    ("low", "res"),
    ("high", "res"),
    ("medium", "res"),
    ("3d", "map"),
    ("3d", "model"),
    ("3", "d", "map"),
    ("3", "d", "model"),
    ("gold", "line"),
    ("low", "poly"),
    ("high", "poly"),
    ("3d",),
    ("3", "d"),
    ("map",),
    ("model",),
    ("preview",),
    ("draft",),
    ("resized",),
)
_CAVE_TERM_LEMMAS = {
    "caves": "cave",
    "chambers": "chamber",
    "ears": "ear",
    "eyes": "eye",
    "holes": "hole",
    "lakes": "lake",
    "mines": "mine",
    "passages": "passage",
    "pits": "pit",
    "rivers": "river",
    "springs": "spring",
    "systems": "system",
}
_QUALIFIER_PREFIXES = {
    "approx": "About",
    "greater_than": "Over",
}


@dataclass(frozen=True)
class CaveMetadataStatistic:
    """One sourced, human-readable statistic for a cave record."""

    label: str
    value: int | float
    unit: str | None = None
    qualifier: str | None = None

    @property
    def display_value(self) -> str:
        """Format the catalog value without making an unsupported claim."""
        if isinstance(self.value, float) and self.value.is_integer():
            value_text = f"{int(self.value):,}"
        elif isinstance(self.value, int):
            value_text = f"{self.value:,}"
        else:
            value_text = f"{self.value:,.12g}"
        text = " ".join(part for part in (value_text, self.unit) if part)
        prefix = _QUALIFIER_PREFIXES.get(self.qualifier or "")
        return f"{prefix} {text}" if prefix else text


@dataclass(frozen=True)
class CaveMetadataSource:
    """One explicitly user-opened external reference for a cave record."""

    title: str
    url: str


@dataclass(frozen=True)
class CaveMetadata:
    """Descriptive metadata that may be associated with a local map name."""

    id: str
    name: str
    aliases: tuple[str, ...]
    continent: str
    country: str
    region: str
    type: str
    statistics: tuple[CaveMetadataStatistic, ...]
    facts: tuple[str, ...]
    sources: tuple[CaveMetadataSource, ...]

    @property
    def location_text(self) -> str:
        """Return the concise geographic label used in rows and details."""
        parts = tuple(
            part
            for part in (self.region, self.country, self.continent)
            if part
        )
        return ", ".join(parts[:2] if len(parts) > 1 else parts)

    @property
    def type_text(self) -> str:
        """Return the catalog type in readable sentence case."""
        return self.type.replace("_", " ").capitalize()

    @property
    def library_detail(self) -> str:
        """Return the stable secondary label for a matched Map Library row."""
        parts = tuple(part for part in (self.location_text, self.type_text) if part)
        return " · ".join(parts)


@dataclass(frozen=True)
class CaveMetadataMatch:
    """A confident catalog association and the conservative route that won."""

    cave: CaveMetadata
    kind: str
    distance: int = 0


class CaveMetadataCatalog:
    """Validate cave metadata and resolve only unambiguous map-name matches."""

    def __init__(self, caves: Iterable[CaveMetadata]) -> None:
        self.caves = tuple(caves)
        self._by_id = {cave.id: cave for cave in self.caves}
        if len(self._by_id) != len(self.caves):
            raise ValueError("cave metadata catalog has duplicate cave ids")

        exact_candidates: dict[str, set[str]] = {}
        self._variants_by_cave_id: dict[str, tuple[str, ...]] = {}
        for cave in self.caves:
            variants = tuple(
                sorted(
                    {
                        variant
                        for name in (cave.name, *cave.aliases)
                        for variant in normalized_name_variants(name)
                    }
                )
            )
            if not variants:
                raise ValueError(f"cave metadata {cave.id!r} has no matchable names")
            self._variants_by_cave_id[cave.id] = variants
            for variant in variants:
                exact_candidates.setdefault(variant, set()).add(cave.id)
        self._exact_candidates = exact_candidates

    @classmethod
    def from_payload(cls, payload: Any) -> "CaveMetadataCatalog":
        """Validate the bundled v1 catalog and return immutable domain data."""
        if not isinstance(payload, dict):
            raise ValueError("cave metadata catalog must be a JSON object")
        if payload.get("schema_version") != _CATALOG_SCHEMA_VERSION:
            raise ValueError(
                "cave metadata catalog must declare schema_version "
                f"{_CATALOG_SCHEMA_VERSION!r}"
            )
        catalog = payload.get("catalog")
        if not isinstance(catalog, dict):
            raise ValueError("cave metadata catalog must contain a catalog object")
        raw_caves = catalog.get("caves")
        if not isinstance(raw_caves, list):
            raise ValueError("cave metadata catalog caves must be a list")
        return cls(_cave_from_payload(raw_cave, index) for index, raw_cave in enumerate(raw_caves))

    def cave(self, cave_id: str | None) -> CaveMetadata | None:
        """Return an exact stable-id lookup without applying name heuristics."""
        if not isinstance(cave_id, str) or not cave_id.strip():
            return None
        return self._by_id.get(cave_id.strip())

    def match(
        self,
        map_name: str,
        *,
        cave_metadata_id: str | None = None,
    ) -> CaveMetadataMatch | None:
        """Match a map title by id, exact normalized name, then bounded edit distance.

        An explicit id always wins.  Names must have one unambiguous exact
        candidate, or one unique fuzzy winner inside a size-dependent small
        edit-distance limit.  A tie or a weak candidate deliberately produces
        no metadata rather than a plausible but misleading cave association.
        """
        explicit = self.cave(cave_metadata_id)
        if explicit is not None:
            return CaveMetadataMatch(explicit, "explicit")
        if not isinstance(map_name, str):
            return None
        query_variants = normalized_name_variants(map_name)
        if not query_variants:
            return None

        for query in query_variants:
            candidates = self._exact_candidates.get(query, set())
            if len(candidates) == 1:
                cave_id = next(iter(candidates))
                return CaveMetadataMatch(self._by_id[cave_id], "exact")
            if len(candidates) > 1:
                return None

        scores: dict[str, int] = {}
        for query in query_variants:
            compact_query = query.replace(" ", "")
            max_distance = _maximum_fuzzy_distance(len(compact_query))
            if max_distance < 0:
                continue
            for cave_id, candidate_variants in self._variants_by_cave_id.items():
                best_for_cave = min(
                    (
                        _bounded_levenshtein_distance(
                            compact_query,
                            candidate.replace(" ", ""),
                            max_distance,
                        )
                        for candidate in candidate_variants
                    ),
                    default=max_distance + 1,
                )
                if best_for_cave <= max_distance:
                    scores[cave_id] = min(scores.get(cave_id, best_for_cave), best_for_cave)

        if not scores:
            return None
        ranked = sorted((distance, cave_id) for cave_id, distance in scores.items())
        best_distance, best_id = ranked[0]
        if len(ranked) > 1 and ranked[1][0] - best_distance < 1:
            return None
        return CaveMetadataMatch(
            self._by_id[best_id],
            "fuzzy",
            distance=best_distance,
        )


def normalized_name_variants(value: str) -> tuple[str, ...]:
    """Return full and safe technical-suffix-stripped forms of a map title."""
    if not isinstance(value, str):
        return ()
    tokens = _normalized_tokens(value)
    if not tokens:
        return ()
    variants = [" ".join(tokens)]
    stripped_tokens = list(tokens)
    while stripped_tokens:
        suffix = next(
            (
                candidate
                for candidate in _TECHNICAL_NAME_SUFFIXES
                if len(stripped_tokens) >= len(candidate)
                and tuple(stripped_tokens[-len(candidate) :]) == candidate
            ),
            None,
        )
        if suffix is None:
            break
        del stripped_tokens[-len(suffix) :]
    if stripped_tokens:
        stripped = " ".join(stripped_tokens)
        if stripped not in variants:
            variants.append(stripped)
    return tuple(variants)


def _normalized_tokens(value: str) -> tuple[str, ...]:
    # Map filenames commonly collapse words into CamelCase.  Split only the
    # safe lower-to-upper boundary before normalizing punctuation and accents.
    spaced_value = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", value)
    decomposed = normalize("NFKD", spaced_value).encode("ascii", "ignore").decode("ascii")
    characters: list[str] = []
    for character in decomposed.casefold():
        if character.isalnum():
            characters.append(character)
        elif character in {"'", "\u2019", "\u2018"}:
            continue
        elif character == "&":
            characters.extend((" ", "a", "n", "d", " "))
        else:
            characters.append(" ")
    return tuple(
        _CAVE_TERM_LEMMAS.get(token, token)
        for token in "".join(characters).split()
    )


def _maximum_fuzzy_distance(length: int) -> int:
    """Keep short cave titles much stricter than longer descriptive names."""
    if length < 5:
        return -1
    if length < 8:
        return 1
    if length < 16:
        return 2
    return 3


def _bounded_levenshtein_distance(left: str, right: str, maximum: int) -> int:
    """Return edit distance, stopping early once it cannot meet ``maximum``."""
    if abs(len(left) - len(right)) > maximum:
        return maximum + 1
    if left == right:
        return 0
    if len(left) > len(right):
        left, right = right, left
    previous = list(range(len(left) + 1))
    for right_index, right_character in enumerate(right, start=1):
        current = [right_index]
        row_minimum = current[0]
        for left_index, left_character in enumerate(left, start=1):
            cost = 0 if left_character == right_character else 1
            value = min(
                current[left_index - 1] + 1,
                previous[left_index] + 1,
                previous[left_index - 1] + cost,
            )
            current.append(value)
            row_minimum = min(row_minimum, value)
        if row_minimum > maximum:
            return maximum + 1
        previous = current
    return previous[-1]


def _cave_from_payload(raw_cave: Any, index: int) -> CaveMetadata:
    if not isinstance(raw_cave, dict):
        raise ValueError(f"cave metadata cave #{index + 1} must be an object")
    description = f"cave metadata cave #{index + 1}"
    return CaveMetadata(
        id=_required_string(raw_cave, "id", description),
        name=_required_string(raw_cave, "name", description),
        aliases=_string_list(raw_cave.get("aliases", ()), "aliases", description),
        continent=_required_string(raw_cave, "continent", description),
        country=_required_string(raw_cave, "country", description),
        region=_required_string(raw_cave, "region", description),
        type=_required_string(raw_cave, "type", description),
        statistics=_statistics_from_payload(raw_cave.get("statistics", ()), description),
        facts=_string_list(raw_cave.get("facts", ()), "facts", description),
        sources=_sources_from_payload(raw_cave.get("sources", ()), description),
    )


def _required_string(raw: dict[str, Any], field_name: str, description: str) -> str:
    value = raw.get(field_name)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{description} has invalid {field_name!r}")
    return value.strip()


def _string_list(raw: Any, field_name: str, description: str) -> tuple[str, ...]:
    if not isinstance(raw, list):
        raise ValueError(f"{description} {field_name!r} must be a list")
    values: list[str] = []
    for value in raw:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{description} has invalid {field_name!r} entry")
        values.append(value.strip())
    return tuple(values)


def _statistics_from_payload(
    raw_statistics: Any,
    description: str,
) -> tuple[CaveMetadataStatistic, ...]:
    if not isinstance(raw_statistics, list):
        raise ValueError(f"{description} 'statistics' must be a list")
    statistics: list[CaveMetadataStatistic] = []
    for index, raw_statistic in enumerate(raw_statistics):
        stat_description = f"{description} statistic #{index + 1}"
        if not isinstance(raw_statistic, dict):
            raise ValueError(f"{stat_description} must be an object")
        value = raw_statistic.get("value")
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or (isinstance(value, float) and not math.isfinite(value))
        ):
            raise ValueError(f"{stat_description} has invalid 'value'")
        unit = _optional_string(raw_statistic.get("unit"), stat_description, "unit")
        qualifier = _optional_string(
            raw_statistic.get("qualifier"),
            stat_description,
            "qualifier",
        )
        statistics.append(
            CaveMetadataStatistic(
                label=_required_string(raw_statistic, "label", stat_description),
                value=value,
                unit=unit,
                qualifier=qualifier,
            )
        )
    return tuple(statistics)


def _sources_from_payload(
    raw_sources: Any,
    description: str,
) -> tuple[CaveMetadataSource, ...]:
    if not isinstance(raw_sources, list):
        raise ValueError(f"{description} 'sources' must be a list")
    sources: list[CaveMetadataSource] = []
    for index, raw_source in enumerate(raw_sources):
        source_description = f"{description} source #{index + 1}"
        if not isinstance(raw_source, dict):
            raise ValueError(f"{source_description} must be an object")
        url = _required_string(raw_source, "url", source_description)
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError(f"{source_description} has an unsafe 'url'")
        sources.append(
            CaveMetadataSource(
                title=_required_string(raw_source, "title", source_description),
                url=url,
            )
        )
    return tuple(sources)


def _optional_string(value: Any, description: str, field_name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{description} has invalid {field_name!r}")
    return value.strip()


@lru_cache(maxsize=1)
def load_bundled_cave_metadata_catalog() -> CaveMetadataCatalog:
    """Load the offline cave catalog packaged with every CaveViewer build."""
    payload = load_bounded_json(
        cave_metadata_catalog_path(),
        max_bytes=_MAX_CATALOG_BYTES,
        description="bundled cave metadata catalog",
    )
    return CaveMetadataCatalog.from_payload(payload)
