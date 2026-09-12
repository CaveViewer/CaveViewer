"""Filesystem paths for resources bundled with the CaveViewer package."""

from importlib.resources import files
from pathlib import Path


def resource_path(*parts: str) -> Path:
    """Return a path to an installed or PyInstaller-bundled resource."""
    resource = files(__name__).joinpath(*parts)
    return Path(str(resource))


def image_path(filename: str) -> Path:
    return resource_path("images", filename)


def ui_icon_path(filename: str) -> Path:
    """Return a path to a bundled UI action icon asset."""
    return resource_path("images", "ui", filename)


INTER_FONT_FILENAMES = {
    "regular": "Inter-Regular.ttf",
    "medium": "Inter-Medium.ttf",
    "semibold": "Inter-SemiBold.ttf",
    "bold": "Inter-Bold.ttf",
}


def inter_font_path(style: str = "regular") -> Path:
    """Return one pinned static Inter face by semantic style name."""
    try:
        filename = INTER_FONT_FILENAMES[style]
    except KeyError as exc:
        expected = ", ".join(INTER_FONT_FILENAMES)
        raise ValueError(f"unknown Inter style {style!r}; expected one of: {expected}") from exc
    return resource_path("fonts", "inter", filename)


def inter_font_paths() -> tuple[Path, ...]:
    """Return every Inter face that must be registered for Tk typography."""
    return tuple(inter_font_path(style) for style in INTER_FONT_FILENAMES)


def inter_font_license_path() -> Path:
    """Return the license shipped with the bundled Inter faces."""
    return resource_path("fonts", "inter", "LICENSE.txt")


def inter_font_manifest_path() -> Path:
    """Return the pinned Inter provenance manifest."""
    return resource_path("fonts", "inter", "manifest.json")


def shader_path(filename: str) -> Path:
    return resource_path("shaders", filename)


RELEASE_PUBLIC_KEY_FILENAMES = {
    "primary": "release_signing_primary_public_key.pem",
    "recovery": "release_signing_recovery_public_key.pem",
    "legacy": "release_signing_legacy_public_key.pem",
}


def release_public_key_path(identity: str = "primary") -> Path:
    """Return one bundled update-manifest trust root by stable identity."""
    try:
        filename = RELEASE_PUBLIC_KEY_FILENAMES[identity]
    except KeyError as exc:
        raise ValueError(f"unknown release public-key identity: {identity}") from exc
    return resource_path(filename)


def map_library_catalog_path() -> Path:
    return resource_path("map_library_catalog.v1.json")


def cave_metadata_catalog_path() -> Path:
    """Return the bundled offline cave metadata catalog."""
    return resource_path("cave_metadata_catalog.v1.json")
