#!/usr/bin/env bash

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
  tmp_file="$(mktemp)"
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
}
