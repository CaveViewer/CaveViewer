"""Versioned, GUI-free visual-branding profile validation and resolution."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import struct
import sys
from types import MappingProxyType
from typing import Mapping

from caveviewer.resources import resource_path


BRANDING_PROFILE_ENVIRONMENT_VARIABLE = "CAVEVIEWER_BRAND_PROFILE"
BRANDING_SCHEMA_VERSION = 1
BRANDING_MANIFEST_FILENAME = "branding.v1.json"
MAX_BRANDING_MANIFEST_BYTES = 64 * 1024
REQUIRED_ROLES = frozenset(
    {
        "application_mark",
        "about_mark",
        "loading_mark",
        "windows_app_icon",
        "macos_app_icon",
        "linux_app_icon",
    }
)

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_ALLOWED_TOP_LEVEL_KEYS = frozenset(
    {
        "schema_version",
        "profile_id",
        "display_name",
        "provenance",
        "assets",
        "roles",
        "loading_ring",
    }
)


class BrandingProfileError(ValueError):
    """Report an unsafe or incomplete branding-profile input."""


@dataclass(frozen=True)
class BrandingAsset:
    """One validated source asset in a branding profile."""

    asset_id: str
    path: Path
    sha256: str
    width: int
    height: int
    alpha_required: bool
    safe_area_inset: float


@dataclass(frozen=True)
class LoadingRingTokens:
    """Bounded brand-controlled colors for loading progress presentation."""

    fill_color: str
    track_color: str


@dataclass(frozen=True)
class BrandingProfile:
    """Immutable semantic branding snapshot resolved from one manifest."""

    profile_id: str
    display_name: str
    manifest_path: Path
    creator: str
    license_name: str
    source: str
    assets: Mapping[str, BrandingAsset]
    roles: Mapping[str, str]
    loading_ring: LoadingRingTokens

    def asset_for(self, role: str) -> BrandingAsset:
        """Return the source asset assigned to one semantic role."""
        try:
            return self.assets[self.roles[role]]
        except KeyError as exc:
            raise BrandingProfileError(f"unknown branding role: {role}") from exc


def default_branding_manifest_path() -> Path:
    """Return the bundled default profile manifest."""
    return resource_path("branding", "default", BRANDING_MANIFEST_FILENAME)


def resolve_branding_profile(
    *,
    environ: Mapping[str, str] | None = None,
    frozen: bool | None = None,
) -> BrandingProfile:
    """Resolve the developer profile override or immutable bundled default.

    Frozen applications deliberately ignore external profile selection so a
    signed package cannot be visually mutated after it was built.
    """
    # Process-environment ownership stays at the application composition edge;
    # callers inject only the mapping they explicitly intend to resolve.
    environment = {} if environ is None else environ
    is_frozen = bool(getattr(sys, "frozen", False)) if frozen is None else frozen
    override = environment.get(BRANDING_PROFILE_ENVIRONMENT_VARIABLE, "").strip()
    if override and not is_frozen:
        return load_branding_profile(_manifest_path(Path(override)))
    return load_branding_profile(default_branding_manifest_path())


def load_branding_profile(manifest_path: str | os.PathLike[str]) -> BrandingProfile:
    """Load and validate one explicit profile manifest without GUI imports."""
    path = Path(manifest_path).resolve()
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise BrandingProfileError(f"cannot read branding manifest {path}: {exc}") from exc
    if len(raw) > MAX_BRANDING_MANIFEST_BYTES:
        raise BrandingProfileError(
            f"branding manifest exceeds {MAX_BRANDING_MANIFEST_BYTES} bytes: {path}"
        )
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BrandingProfileError(f"branding manifest is not valid UTF-8 JSON: {path}") from exc
    if not isinstance(payload, dict):
        raise BrandingProfileError("branding manifest must contain a JSON object")
    unknown_keys = set(payload) - _ALLOWED_TOP_LEVEL_KEYS
    if unknown_keys:
        raise BrandingProfileError(
            f"branding manifest contains unknown keys: {sorted(unknown_keys)}"
        )
    if payload.get("schema_version") != BRANDING_SCHEMA_VERSION:
        raise BrandingProfileError(
            f"branding schema_version must be {BRANDING_SCHEMA_VERSION}"
        )

    profile_id = _identifier(payload.get("profile_id"), "profile_id")
    display_name = _non_empty_text(payload.get("display_name"), "display_name")
    provenance = _object(payload.get("provenance"), "provenance")
    creator = _non_empty_text(provenance.get("creator"), "provenance.creator")
    license_name = _non_empty_text(
        provenance.get("license"), "provenance.license"
    )
    source = _non_empty_text(provenance.get("source"), "provenance.source")

    asset_payloads = _object(payload.get("assets"), "assets")
    if not asset_payloads:
        raise BrandingProfileError("assets must declare at least one source asset")
    assets = {
        _identifier(asset_id, "asset id"): _load_asset(
            path.parent,
            _identifier(asset_id, "asset id"),
            asset_payload,
        )
        for asset_id, asset_payload in asset_payloads.items()
    }

    role_payloads = _object(payload.get("roles"), "roles")
    missing_roles = REQUIRED_ROLES - set(role_payloads)
    unknown_roles = set(role_payloads) - REQUIRED_ROLES
    if missing_roles or unknown_roles:
        details = []
        if missing_roles:
            details.append(f"missing {sorted(missing_roles)}")
        if unknown_roles:
            details.append(f"unknown {sorted(unknown_roles)}")
        raise BrandingProfileError("invalid branding roles: " + ", ".join(details))
    roles: dict[str, str] = {}
    for role, asset_id_value in role_payloads.items():
        asset_id = _identifier(asset_id_value, f"roles.{role}")
        if asset_id not in assets:
            raise BrandingProfileError(
                f"branding role {role} references unknown asset {asset_id}"
            )
        roles[role] = asset_id

    loading_ring_payload = _object(payload.get("loading_ring"), "loading_ring")
    loading_ring = LoadingRingTokens(
        fill_color=_color(loading_ring_payload.get("fill_color"), "fill_color"),
        track_color=_color(loading_ring_payload.get("track_color"), "track_color"),
    )
    return BrandingProfile(
        profile_id=profile_id,
        display_name=display_name,
        manifest_path=path,
        creator=creator,
        license_name=license_name,
        source=source,
        assets=MappingProxyType(assets),
        roles=MappingProxyType(roles),
        loading_ring=loading_ring,
    )


def _manifest_path(path: Path) -> Path:
    return path / BRANDING_MANIFEST_FILENAME if path.is_dir() else path


def _load_asset(profile_root: Path, asset_id: str, payload) -> BrandingAsset:
    value = _object(payload, f"assets.{asset_id}")
    relative_text = _non_empty_text(value.get("path"), f"assets.{asset_id}.path")
    relative_path = Path(relative_text)
    if relative_path.is_absolute() or ".." in relative_path.parts:
        raise BrandingProfileError(f"asset {asset_id} path must stay inside its profile")
    asset_path = (profile_root / relative_path).resolve()
    try:
        asset_path.relative_to(profile_root.resolve())
    except ValueError as exc:
        raise BrandingProfileError(f"asset {asset_id} path escapes its profile") from exc
    expected_hash = _sha256(value.get("sha256"), f"assets.{asset_id}.sha256")
    try:
        file_hash = hashlib.sha256(asset_path.read_bytes()).hexdigest()
    except OSError as exc:
        raise BrandingProfileError(f"cannot read branding asset {asset_path}: {exc}") from exc
    if file_hash != expected_hash:
        raise BrandingProfileError(
            f"branding asset {asset_id} SHA-256 mismatch: expected {expected_hash}, got {file_hash}"
        )

    width, height, has_alpha = _read_png_header(asset_path)
    minimum_width = _positive_int(
        value.get("minimum_width"), f"assets.{asset_id}.minimum_width"
    )
    minimum_height = _positive_int(
        value.get("minimum_height"), f"assets.{asset_id}.minimum_height"
    )
    if width < minimum_width or height < minimum_height:
        raise BrandingProfileError(
            f"branding asset {asset_id} is {width}x{height}; requires at least "
            f"{minimum_width}x{minimum_height}"
        )
    if abs((width / height) - 1.0) > 0.001:
        raise BrandingProfileError(f"branding asset {asset_id} must be square")
    alpha_required = value.get("alpha") == "required"
    if value.get("alpha") not in {"required", "optional"}:
        raise BrandingProfileError(
            f"assets.{asset_id}.alpha must be 'required' or 'optional'"
        )
    if alpha_required and not has_alpha:
        raise BrandingProfileError(f"branding asset {asset_id} requires an alpha channel")
    safe_area = value.get("safe_area_inset")
    if isinstance(safe_area, bool) or not isinstance(safe_area, (int, float)):
        raise BrandingProfileError(
            f"assets.{asset_id}.safe_area_inset must be a number"
        )
    safe_area_value = float(safe_area)
    if not 0.0 <= safe_area_value <= 0.25:
        raise BrandingProfileError(
            f"assets.{asset_id}.safe_area_inset must be between 0 and 0.25"
        )
    return BrandingAsset(
        asset_id=asset_id,
        path=asset_path,
        sha256=file_hash,
        width=width,
        height=height,
        alpha_required=alpha_required,
        safe_area_inset=safe_area_value,
    )


def _read_png_header(path: Path) -> tuple[int, int, bool]:
    with path.open("rb") as handle:
        header = handle.read(26)
    if len(header) != 26 or header[:8] != _PNG_SIGNATURE or header[12:16] != b"IHDR":
        raise BrandingProfileError(f"branding asset must be a valid PNG: {path}")
    width, height = struct.unpack(">II", header[16:24])
    color_type = header[25]
    return width, height, color_type in {4, 6}


def _object(value, field: str) -> dict:
    if not isinstance(value, dict):
        raise BrandingProfileError(f"{field} must be an object")
    return value


def _non_empty_text(value, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise BrandingProfileError(f"{field} must be non-empty text")
    return value.strip()


def _identifier(value, field: str) -> str:
    text = _non_empty_text(value, field)
    if not all(character.islower() or character.isdigit() or character in "-_" for character in text):
        raise BrandingProfileError(
            f"{field} must contain only lowercase letters, digits, hyphens, or underscores"
        )
    return text


def _sha256(value, field: str) -> str:
    text = _non_empty_text(value, field)
    if len(text) != 64 or any(character not in "0123456789abcdef" for character in text):
        raise BrandingProfileError(f"{field} must be a lowercase SHA-256 digest")
    return text


def _positive_int(value, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise BrandingProfileError(f"{field} must be a positive integer")
    return value


def _color(value, field: str) -> str:
    text = _non_empty_text(value, f"loading_ring.{field}")
    if len(text) != 7 or text[0] != "#" or any(
        character not in "0123456789abcdefABCDEF" for character in text[1:]
    ):
        raise BrandingProfileError(
            f"loading_ring.{field} must be a six-digit hexadecimal color"
        )
    return text.upper()
