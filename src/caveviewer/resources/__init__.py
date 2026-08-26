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
    """Return a path to a scalable UI action icon source asset."""
    return resource_path("images", "ui", filename)


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
