#!/usr/bin/env bash

cv_sha256() {
  local path="$1"
  shasum -a 256 "$path" | awk '{print $1}'
}

cv_size_bytes() {
  local path="$1"
  wc -c < "$path" | tr -d ' '
}

cv_created_at_utc() {
  date -u +"%Y-%m-%dT%H:%M:%SZ"
}
