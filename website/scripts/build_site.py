#!/usr/bin/env python3
"""Build the bounded production Pages artifact from the website source."""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path


WEBSITE_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = WEBSITE_ROOT.parent
DEVELOPMENT_ROOT = REPOSITORY_ROOT / "docs" / "development"
PUBLIC_PAGES = (
    "about.html",
    "advantage.html",
    "contact.html",
    "docs.html",
    "index.html",
    "media.html",
    "sponsors.html",
)


def _inside(path: Path, parent: Path) -> bool:
    return path == parent or parent in path.parents


def _require_source(path: Path) -> None:
    if not path.exists():
        raise ValueError(f"required source path is missing: {path}")


def _prepare_output(output: Path, *, replace: bool) -> None:
    if _inside(output, REPOSITORY_ROOT):
        raise ValueError("artifact output must be outside the repository")
    if output == output.parent:
        raise ValueError("artifact output cannot be a filesystem root")
    if output.exists():
        if not output.is_dir():
            raise ValueError(f"artifact output is not a directory: {output}")
        if any(output.iterdir()):
            if not replace:
                raise ValueError(
                    "artifact output is not empty; pass --replace to clear it"
                )
            shutil.rmtree(output)
    output.mkdir(parents=True, exist_ok=True)


def build_site(output: Path, *, replace: bool = False) -> Path:
    """Copy only the intended public routes and retained development documents."""

    output = output.expanduser().resolve()
    _require_source(DEVELOPMENT_ROOT)
    for page_name in PUBLIC_PAGES:
        _require_source(WEBSITE_ROOT / page_name)
    for directory_name in ("assets", "storage"):
        _require_source(WEBSITE_ROOT / directory_name)
    _require_source(WEBSITE_ROOT / "CNAME")

    _prepare_output(output, replace=replace)
    for page_name in PUBLIC_PAGES:
        shutil.copy2(WEBSITE_ROOT / page_name, output / page_name)
    shutil.copy2(WEBSITE_ROOT / "CNAME", output / "CNAME")
    for directory_name in ("assets", "storage"):
        shutil.copytree(WEBSITE_ROOT / directory_name, output / directory_name)
    shutil.copytree(DEVELOPMENT_ROOT, output / "development")
    return output


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        required=True,
        type=Path,
        help="empty directory outside the repository for the Pages artifact",
    )
    parser.add_argument(
        "--replace",
        action="store_true",
        help="clear a non-empty output directory before building",
    )
    args = parser.parse_args(argv)

    try:
        artifact = build_site(args.output, replace=args.replace)
    except ValueError as error:
        parser.error(str(error))
    print(artifact)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
