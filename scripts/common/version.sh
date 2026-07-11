#!/usr/bin/env bash

# Version helper functions.
# Source this file from release scripts that need to read or update
# src/caveviewer/version.py.

cv_read_app_field() {
  local version_file="$1"
  local field_name="$2"
  sed -n "s/^${field_name} = \"\([^\"]*\)\"$/\1/p" "$version_file" | head -n 1
}

cv_read_app_version() {
  local version_file="$1"
  cv_read_app_field "$version_file" "APP_VERSION"
}

cv_read_app_name() {
  local version_file="$1"
  cv_read_app_field "$version_file" "APP_NAME"
}

cv_set_app_version() {
  local version_file="$1"
  local target_version="$2"
  local tmp_file
  tmp_file="$(mktemp "${version_file}.tmp.XXXXXX")"
  chmod --reference="$version_file" "$tmp_file" 2>/dev/null || chmod 0644 "$tmp_file"
  awk -v target="$target_version" '
    BEGIN { replaced = 0 }
    /^APP_VERSION = "/ && replaced == 0 {
      print "APP_VERSION = \"" target "\""
      replaced = 1
      next
    }
    { print }
  ' "$version_file" > "$tmp_file"
  mv "$tmp_file" "$version_file"

  # Keep AppStream release history synchronized with the version source.  The
  # metadata file is optional for consumers that reuse this helper in fixtures.
  local repo_root
  local metainfo_file
  repo_root="$(cd "$(dirname "$version_file")/../.." && pwd)"
  metainfo_file="$repo_root/packaging/linux/io.github.kernalpanic.caveviewer.metainfo.xml"
  if [ -f "$metainfo_file" ] && \
      ! grep -q "<release version=\"${target_version}\"" "$metainfo_file"; then
    local metainfo_tmp
    metainfo_tmp="$(mktemp "${metainfo_file}.tmp.XXXXXX")"
    chmod --reference="$metainfo_file" "$metainfo_tmp" 2>/dev/null || chmod 0644 "$metainfo_tmp"
    awk -v target="$target_version" -v release_date="$(date -u +%F)" '
      /<releases>/ && inserted == 0 {
        print
        print "    <release version=\"" target "\" date=\"" release_date "\" />"
        inserted = 1
        next
      }
      { print }
    ' "$metainfo_file" > "$metainfo_tmp"
    mv "$metainfo_tmp" "$metainfo_file"
  fi
}
