#!/usr/bin/env bash

# Shared macOS architecture normalization for build and release scripts.

cv_normalize_macos_arch() {
  case "$1" in
    arm64|aarch64)
      echo "arm64"
      ;;
    x86_64|amd64|x64)
      echo "x86_64"
      ;;
    *)
      return 1
      ;;
  esac
}

cv_detect_macos_arch() {
  local detected
  detected="$(cv_normalize_macos_arch "$(uname -m)")" || {
    echo "Error: unsupported macOS architecture: $(uname -m)" >&2
    return 1
  }
  echo "$detected"
}

cv_resolve_macos_arch() {
  local requested="${1:-}"
  if [ -z "$requested" ]; then
    requested="${CAVEVIEWER_MACOS_ARCH:-}"
  fi
  if [ -z "$requested" ]; then
    cv_detect_macos_arch
    return
  fi

  local normalized
  normalized="$(cv_normalize_macos_arch "$requested")" || {
    echo "Error: invalid macOS architecture '$requested' (expected arm64 or x86_64)" >&2
    return 1
  }
  echo "$normalized"
}

cv_require_macos_host_arch() {
  local requested="$1"
  local host_arch
  host_arch="$(cv_detect_macos_arch)"
  if [ "$requested" != "$host_arch" ]; then
    echo "Error: cannot build macOS $requested artifacts on a $host_arch process." >&2
    echo "Run the build on the matching architecture (Rosetta x86_64 is supported)." >&2
    return 1
  fi
}
