"""Read immutable release metadata bundled with one CaveViewer package.

Packaging writes one small JSON resource into every frozen application payload.
Application composition reads it once and passes the resulting immutable value
to runtime-settings resolution.  The loader has no GUI, Tk, OpenGL, network,
or environment dependency, so source and unit-test callers can inject a
metadata value without relying on a packaged executable.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from caveviewer.resources import resource_path


RELEASE_METADATA_RESOURCE_NAME = "release_metadata.v1.json"
RELEASE_METADATA_SCHEMA_VERSION = 1
VALID_RELEASE_CHANNELS = frozenset({"stable", "prerelease"})


class ReleaseMetadataSource(str, Enum):
    """Describe where one release-channel value came from."""

    BUNDLED = "bundled"
    SOURCE_DEFAULT = "source_default"
    FALLBACK_MISSING = "fallback_missing"
    FALLBACK_INVALID = "fallback_invalid"
    FALLBACK_UNREADABLE = "fallback_unreadable"


@dataclass(frozen=True, slots=True)
class ReleaseMetadata:
    """Immutable package-level release metadata for one application process."""

    release_channel: str
    source: ReleaseMetadataSource
    diagnostic: str | None = None

    def __post_init__(self) -> None:
        release_channel = str(self.release_channel).strip().lower()
        if release_channel not in VALID_RELEASE_CHANNELS:
            raise ValueError(
                "release channel must be one of: "
                + ", ".join(sorted(VALID_RELEASE_CHANNELS))
            )
        object.__setattr__(self, "release_channel", release_channel)
        diagnostic = None if self.diagnostic is None else str(self.diagnostic).strip()
        object.__setattr__(self, "diagnostic", diagnostic or None)


def default_release_metadata() -> ReleaseMetadata:
    """Return the stable source-checkout default without filesystem access."""

    return ReleaseMetadata(
        release_channel="stable",
        source=ReleaseMetadataSource.SOURCE_DEFAULT,
    )


def load_embedded_release_metadata(
    metadata_path: str | Path | None = None,
) -> ReleaseMetadata:
    """Load packaged metadata, falling back safely for old or source builds.

    A missing resource is expected for source checkouts and historical frozen
    packages.  It resolves to the stable channel because that was their
    established behavior.  Malformed or unreadable metadata does not prevent
    the offline viewer from starting, but records an explicit diagnostic for
    the composition boundary to log.
    """

    path = (
        Path(metadata_path)
        if metadata_path is not None
        else resource_path(RELEASE_METADATA_RESOURCE_NAME)
    )
    try:
        raw_metadata = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ReleaseMetadata(
            release_channel="stable",
            source=ReleaseMetadataSource.FALLBACK_MISSING,
            diagnostic="Embedded release metadata is missing; using stable updates.",
        )
    except OSError as exc:
        return ReleaseMetadata(
            release_channel="stable",
            source=ReleaseMetadataSource.FALLBACK_UNREADABLE,
            diagnostic=(
                "Embedded release metadata could not be read; "
                f"using stable updates ({exc})."
            ),
        )

    try:
        payload = json.loads(raw_metadata)
        release_channel = _release_channel_from_payload(payload)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        return ReleaseMetadata(
            release_channel="stable",
            source=ReleaseMetadataSource.FALLBACK_INVALID,
            diagnostic=(
                "Embedded release metadata is invalid; "
                f"using stable updates ({exc})."
            ),
        )

    return ReleaseMetadata(
        release_channel=release_channel,
        source=ReleaseMetadataSource.BUNDLED,
    )


def _release_channel_from_payload(payload: object) -> str:
    """Validate schema version 1 and return its normalized release channel."""

    if not isinstance(payload, dict):
        raise ValueError("release metadata root must be a JSON object")
    schema_version = payload.get("schema_version")
    if (
        type(schema_version) is not int
        or schema_version != RELEASE_METADATA_SCHEMA_VERSION
    ):
        raise ValueError(
            "release metadata schema_version must be "
            f"{RELEASE_METADATA_SCHEMA_VERSION}"
        )
    release_channel = payload.get("release_channel")
    if not isinstance(release_channel, str):
        raise ValueError("release metadata release_channel must be a string")
    normalized = release_channel.strip().lower()
    if normalized not in VALID_RELEASE_CHANNELS:
        raise ValueError(
            "release metadata release_channel must be one of: "
            + ", ".join(sorted(VALID_RELEASE_CHANNELS))
        )
    return normalized
