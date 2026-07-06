#!/usr/bin/env bash
set -euo pipefail

# Writes updates/macos/stable.json for the in-app updater.
# Usage:
#   ./scripts/macos/update_manifest.sh <version> <macos_dmg_url> <macos_dmg_file> [release_notes]
# Example:
#   ./scripts/macos/update_manifest.sh 1.0.1 \
#     "https://github.com/<owner>/CaveViewerPlus/releases/download/v1.0.1/CaveViewer-1.0.1.dmg" \
#     "dist/macos/packages/CaveViewer-1.0.1.dmg" \
#     "Bug fixes and performance improvements"

if [ "$#" -lt 3 ]; then
  echo "Usage: $0 <version> <macos_dmg_url> <macos_dmg_file> [release_notes]"
  exit 1
fi

version="$1"
macos_dmg_url="$2"
macos_dmg_file="$3"
release_notes="${4:-}"

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "$script_dir/../.." && pwd)"
source "$repo_root/scripts/common/artifacts.sh"
manifest_path="$repo_root/updates/macos/stable.json"

macos_dmg_size_bytes="null"
macos_dmg_sha256_value=""

if [ -n "$macos_dmg_file" ]; then
  if [ ! -f "$macos_dmg_file" ]; then
    echo "Error: macOS DMG file not found: $macos_dmg_file"
    exit 1
  fi
  macos_dmg_size_bytes="$(cv_size_bytes "$macos_dmg_file")"
  macos_dmg_sha256_value="$(cv_sha256 "$macos_dmg_file")"
fi

mkdir -p "$(dirname "$manifest_path")"
cat > "$manifest_path" <<EOF
{
  "latest_version": "$version",
  "download_url": "$macos_dmg_url",
  "download_size_bytes": $macos_dmg_size_bytes,
  "download_url_macosx_dmg": "$macos_dmg_url",
  "download_size_bytes_macosx_dmg": $macos_dmg_size_bytes,
  "release_notes": "$release_notes",
  "sha256": "$macos_dmg_sha256_value",
  "sha256_macosx_dmg": "$macos_dmg_sha256_value"
}
EOF

echo "Wrote manifest: $manifest_path"
