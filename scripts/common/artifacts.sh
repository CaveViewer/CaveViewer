#!/usr/bin/env bash

# Artifact helper functions.
# Source this file from packaging and manifest scripts that need checksums,
# byte sizes, or UTC timestamps.

cv_sha256() {
  local path="$1"
  if command -v shasum >/dev/null 2>&1; then
    shasum -a 256 "$path" | awk '{print $1}'
  elif command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$path" | awk '{print $1}'
  elif command -v python3 >/dev/null 2>&1; then
    python3 - "$path" <<'PY'
import hashlib
import pathlib
import sys

hasher = hashlib.sha256()
with pathlib.Path(sys.argv[1]).open("rb") as handle:
    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
        hasher.update(chunk)
print(hasher.hexdigest())
PY
  elif command -v python >/dev/null 2>&1; then
    python - "$path" <<'PY'
import hashlib
import pathlib
import sys

hasher = hashlib.sha256()
with pathlib.Path(sys.argv[1]).open("rb") as handle:
    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
        hasher.update(chunk)
print(hasher.hexdigest())
PY
  else
    echo "Error: no SHA-256 tool found. Install shasum, sha256sum, or Python." >&2
    return 1
  fi
}

cv_size_bytes() {
  local path="$1"
  wc -c < "$path" | tr -d ' '
}

cv_created_at_utc() {
  date -u +"%Y-%m-%dT%H:%M:%SZ"
}
