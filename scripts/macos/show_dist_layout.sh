#!/usr/bin/env bash
set -euo pipefail

# Prints the expected DMG dist artifacts for the current app version
# and shows what actually exists under dist/.

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "$script_dir/../.." && pwd)"
source "$repo_root/scripts/common/version.sh"
version_file="$repo_root/caveviewer_version.py"

dist_root="$repo_root/dist"
dist_macos_root="$dist_root/macos"
dist_app_dir="$dist_macos_root/app"
dist_packages_dir="$dist_macos_root/packages"
dist_metadata_dir="$dist_macos_root/metadata"
if [ ! -f "$version_file" ]; then
  echo "Error: version file not found: $version_file"
  exit 1
fi

version="$(cv_read_app_version "$version_file")"
if [ -z "$version" ]; then
  echo "Error: could not parse APP_VERSION from $version_file"
  exit 1
fi

expected_app_dmg="$dist_packages_dir/CaveViewer-${version}.dmg"
expected_app_meta="$dist_metadata_dir/CaveViewer-${version}.json"

print_status() {
  local path="$1"
  if [ -e "$path" ]; then
    echo "[OK]     $path"
  else
    echo "[MISSING] $path"
  fi
}

echo "CaveViewer dist artifact summary"
echo "Version: $version"
echo

echo "Expected artifacts"
print_status "$expected_app_dmg"
print_status "$expected_app_meta"
echo

echo "Current dist tree"
if [ -d "$dist_root" ]; then
  find "$dist_root" -maxdepth 4 \( -type d -o -type f \) | sed "s|$repo_root/||" | sort
else
  echo "dist/ does not exist yet"
fi
