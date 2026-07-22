"""Filesystem paths for resources bundled with the CaveViewer package."""

from importlib.resources import files
from pathlib import Path


def resource_path(*parts: str) -> Path:
    """Return a path to an installed or PyInstaller-bundled resource."""
    resource = files(__name__).joinpath(*parts)
    return Path(str(resource))


def image_path(filename: str) -> Path:
    return resource_path("images", filename)


def shader_path(filename: str) -> Path:
    return resource_path("shaders", filename)


def release_public_key_path() -> Path:
    return resource_path("release_signing_public_key.pem")


def map_library_catalog_path() -> Path:
    return resource_path("map_library_catalog.v1.json")
