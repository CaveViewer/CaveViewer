#!/usr/bin/env bash
set -euo pipefail

# Writes the Linux in-app updater manifest.
# Usage:
#   ./scripts/linux/common/update_manifest.sh <version> <appimage_url> <appimage_file> [release_notes]
# Example:
#   ./scripts/linux/common/update_manifest.sh 1.0.1 \
#     "https://github.com/<owner>/CaveViewerPlus/releases/download/v1.0.1/CaveViewer-1.0.1-x86_64.AppImage" \
#     "dist/linux/x86_64/packages/CaveViewer-1.0.1-x86_64.AppImage" \
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
repo_root="$(cd "$script_dir/../../.." && pwd)"
source "$repo_root/scripts/common/artifacts.sh"

linux_update_arch="${CAVEVIEWER_LINUX_UPDATE_ARCH:-}"
case "$linux_update_arch" in
  arm64) manifest_arch_dir="arm64" ;;
  amd64|x86|x86_64) manifest_arch_dir="x86_64" ;;
  "")
    case "$(uname -m)" in
      aarch64|arm64) manifest_arch_dir="arm64" ;;
      x86_64|amd64) manifest_arch_dir="x86_64" ;;
      *)
        echo "Error: could not determine Linux update manifest architecture. Set CAVEVIEWER_LINUX_UPDATE_ARCH=arm64 or x86_64."
        exit 1
        ;;
    esac
    ;;
  *)
    echo "Error: invalid CAVEVIEWER_LINUX_UPDATE_ARCH '$linux_update_arch' (expected arm64, amd64, x86, or x86_64)"
    exit 1
    ;;
esac

manifest_path="$repo_root/updates/linux/$manifest_arch_dir/stable.json"

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
