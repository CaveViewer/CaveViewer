"""Tests for release-specific AppStream metadata comparison."""

import pytest

from scripts.common.release_metadata import appstream_releases_changed


BASE_METAINFO = """\
<component>
  <project_license>GPL-3.0-only</project_license>
  <releases>
    <release version="1.0.0" date="2026-01-01" />
  </releases>
</component>
"""


def test_project_license_change_is_not_a_release_change():
    updated = BASE_METAINFO.replace("GPL-3.0-only", "AGPL-3.0-only")

    assert not appstream_releases_changed(BASE_METAINFO, updated)


def test_new_appstream_release_is_a_release_change():
    updated = BASE_METAINFO.replace(
        "  <releases>\n",
        '  <releases>\n    <release version="1.0.1" date="2026-01-02" />\n',
    )

    assert appstream_releases_changed(BASE_METAINFO, updated)


def test_missing_appstream_releases_is_rejected():
    with pytest.raises(ValueError, match="has no releases element"):
        appstream_releases_changed(BASE_METAINFO, "<component />")
