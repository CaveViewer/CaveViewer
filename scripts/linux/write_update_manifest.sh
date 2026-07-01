#!/usr/bin/env bash
set -euo pipefail

# Writes updates/linux/stable.json for the in-app updater.
# Usage:
#   ./scripts/linux/write_update_manifest.sh <version> <appimage_url> <appimage_file> [release_notes]
# Example:
#   ./scripts/linux/write_update_manifest.sh 1.0.1 \
#     "https://github.com/<owner>/CaveViewerPlus/releases/download/v1.0.1/CaveViewer-1.0.1-x86_64.AppImage" \
#     "dist/linux/packages/CaveViewer-1.0.1-x86_64.AppImage" \
#     "Bug fixes and performance improvements"

if [ "$#" -lt 3 ]; then
  echo "Usage: $0 <version> <appimage_url> <appimage_file> [release_notes]"
  exit 1
fi

version="$1"
appimage_url="$2"
appimage_file="$3"
release_notes="${4:-}"

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "$script_dir/../.." && pwd)"
source "$repo_root/scripts/common/artifacts.sh"
manifest_path="$repo_root/updates/linux/stable.json"

appimage_size_bytes="null"
appimage_sha256_value=""

if [ -n "$appimage_file" ]; then
  if [ ! -f "$appimage_file" ]; then
    echo "Error: AppImage file not found: $appimage_file"
    exit 1
  fi
  appimage_size_bytes="$(cv_size_bytes "$appimage_file")"
  appimage_sha256_value="$(cv_sha256 "$appimage_file")"
fi

mkdir -p "$(dirname "$manifest_path")"
cat > "$manifest_path" <<EOF
{
  "latest_version": "$version",
  "download_url": "$appimage_url",
  "download_size_bytes": $appimage_size_bytes,
  "download_url_linux_appimage": "$appimage_url",
  "download_size_bytes_linux_appimage": $appimage_size_bytes,
  "release_notes": "$release_notes",
  "sha256": "$appimage_sha256_value",
  "sha256_linux_appimage": "$appimage_sha256_value"
}
EOF

echo "Wrote manifest: $manifest_path"
