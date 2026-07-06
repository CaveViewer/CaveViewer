#!/usr/bin/env bash
set -euo pipefail

# Writes updates/windows/stable.json for the in-app updater.
# Usage:
#   ./scripts/windows/update_manifest.sh <version> <windows_zip_url> <windows_zip_file> [release_notes]
# Example:
#   ./scripts/windows/update_manifest.sh 1.0.1 \
#     "https://github.com/<owner>/CaveViewerPlus/releases/download/v1.0.1/CaveViewer-1.0.1-windows.zip" \
#     "dist/windows/packages/CaveViewer-1.0.1-windows.zip" \
#     "Bug fixes and performance improvements"

if [ "$#" -lt 3 ]; then
  echo "Usage: $0 <version> <windows_zip_url> <windows_zip_file> [release_notes]"
  exit 1
fi

version="$1"
windows_zip_url="$2"
windows_zip_file="$3"
release_notes="${4:-}"

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "$script_dir/../.." && pwd)"
source "$repo_root/scripts/common/artifacts.sh"
manifest_path="$repo_root/updates/windows/stable.json"

windows_zip_size_bytes="null"
windows_zip_sha256_value=""

if [ -n "$windows_zip_file" ]; then
  if [ ! -f "$windows_zip_file" ]; then
    echo "Error: Windows zip file not found: $windows_zip_file"
    exit 1
  fi
  windows_zip_size_bytes="$(cv_size_bytes "$windows_zip_file")"
  windows_zip_sha256_value="$(cv_sha256 "$windows_zip_file")"
fi

mkdir -p "$(dirname "$manifest_path")"
cat > "$manifest_path" <<EOF
{
  "latest_version": "$version",
  "download_url": "$windows_zip_url",
  "download_size_bytes": $windows_zip_size_bytes,
  "download_url_windows_zip": "$windows_zip_url",
  "download_size_bytes_windows_zip": $windows_zip_size_bytes,
  "release_notes": "$release_notes",
  "sha256": "$windows_zip_sha256_value",
  "sha256_windows_zip": "$windows_zip_sha256_value"
}
EOF

echo "Wrote manifest: $manifest_path"
