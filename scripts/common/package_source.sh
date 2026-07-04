#!/usr/bin/env bash
set -euo pipefail

# Package source code for GitHub releases
# Usage:
#   ./scripts/common/package_source.sh <version>
# Example:
#   ./scripts/common/package_source.sh 1.2.45

if [ "$#" -lt 1 ]; then
  echo "Usage: $0 <version>"
  exit 1
fi

version="$1"
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "$script_dir/../.." && pwd)"
dist_dir="$repo_root/dist/source"

normalized_version="${version#v}"
source_tarball="$dist_dir/CaveViewer-${normalized_version}-source.tar.gz"

mkdir -p "$dist_dir"

echo "Packaging source code v$normalized_version..."

# Create tarball, excluding build artifacts and cache
# Use a temp directory to wrap sources in version-named folder (works on both GNU and BSD tar)
temp_root=$(mktemp -d)
trap "rm -rf $temp_root" EXIT

source_dir="$temp_root/CaveViewer-${normalized_version}"
mkdir -p "$source_dir"

cd "$repo_root"

# Copy only files/directories that exist
for item in \
  caveviewer.py \
  caveviewer_version.py \
  requirements.txt \
  README.md \
  LICENSE \
  THIRD_PARTY_NOTICES.md \
  CHANGELOG.md \
  core \
  gui \
  shaders \
  scripts \
  updates \
  docs; do
  
  if [ -e "$item" ]; then
    if [ -d "$item" ]; then
      mkdir -p "$source_dir/$item"
      rsync -a --delete \
        --exclude='.git' \
        --exclude='build' \
        --exclude='dist' \
        --exclude='.pytest_cache' \
        --exclude='__pycache__' \
        --exclude='*.pyc' \
        --exclude='.DS_Store' \
        --exclude='_cache' \
        --exclude='.caveviewer_cache' \
        --exclude='venv' \
        --exclude='.venv*' \
        --exclude='*.egg-info' \
        --exclude='.env' \
        "$item/" "$source_dir/$item/"
    else
      cp "$item" "$source_dir/"
    fi
  fi
done

# Create tarball from temp directory (now it has the version prefix built-in)
cd "$temp_root"
tar -czf "$source_tarball" "CaveViewer-${normalized_version}/"

if [ ! -f "$source_tarball" ]; then
  echo "Error: source tarball creation failed"
  exit 1
fi

size_bytes=$(stat -c%s "$source_tarball" 2>/dev/null || stat -f%z "$source_tarball")
sha256_value=$(shasum -a 256 "$source_tarball" | cut -d' ' -f1)

echo ""
echo "Source tarball created successfully!"
echo "  File: $source_tarball"
echo "  Size: $(numfmt --to=iec-i --suffix=B $size_bytes 2>/dev/null || echo "$size_bytes bytes")"
echo "  SHA256: $sha256_value"
