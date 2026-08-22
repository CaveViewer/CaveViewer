#!/usr/bin/env bash

# Build-time release-channel metadata shared by every frozen package path.
#
# This file intentionally owns only the two release channels and the generated
# resource contract.  It is sourced by release.sh and the platform scripts so
# direct package builds have the same safe stable default as dispatcher builds.

CV_RELEASE_METADATA_RESOURCE_NAME="release_metadata.v1.json"

cv_release_channel() {
  local release_channel="${CAVEVIEWER_BUILD_RELEASE_CHANNEL:-stable}"

  case "$release_channel" in
    stable|preview)
      printf '%s\n' "$release_channel"
      ;;
    *)
      echo "Error: CAVEVIEWER_BUILD_RELEASE_CHANNEL must be stable or preview." >&2
      return 1
      ;;
  esac
}

cv_write_release_metadata() {
  local output_path="$1"
  local release_channel output_dir temporary_path

  release_channel="$(cv_release_channel)" || return 1
  output_dir="$(dirname "$output_path")"
  mkdir -p "$output_dir"
  temporary_path="$output_path.tmp.$$"

  printf '{\n  "schema_version": 1,\n  "release_channel": "%s"\n}\n' \
    "$release_channel" > "$temporary_path"
  mv -f "$temporary_path" "$output_path"
}

cv_verify_release_metadata() {
  local metadata_path="$1"
  local expected_channel="$2"

  if [ ! -f "$metadata_path" ]; then
    echo "Error: embedded release metadata is missing: $metadata_path" >&2
    return 1
  fi
  if ! grep -Eq '"schema_version"[[:space:]]*:[[:space:]]*1([[:space:],}]|$)' "$metadata_path"; then
    echo "Error: embedded release metadata has an unsupported schema: $metadata_path" >&2
    return 1
  fi
  if ! grep -Fq "\"release_channel\": \"$expected_channel\"" "$metadata_path"; then
    echo "Error: embedded release metadata channel does not match '$expected_channel': $metadata_path" >&2
    return 1
  fi
}

cv_prepare_release_metadata() {
  local repo_root="$1"
  local release_channel metadata_path

  release_channel="$(cv_release_channel)" || return 1
  metadata_path="${CAVEVIEWER_RELEASE_METADATA_PATH:-}"
  if [ -n "$metadata_path" ]; then
    cv_verify_release_metadata "$metadata_path" "$release_channel" || return 1
  else
    metadata_path="$repo_root/build/$CV_RELEASE_METADATA_RESOURCE_NAME"
    cv_write_release_metadata "$metadata_path" || return 1
  fi

  export CAVEVIEWER_RELEASE_METADATA_PATH="$metadata_path"
  printf '%s\n' "$metadata_path"
}
