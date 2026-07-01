#!/usr/bin/env bash
set -euo pipefail

# Packages dist/macos/app/CaveViewer.app into a versioned DMG and includes
# README.md inside the DMG for end-user instructions.

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "$script_dir/../.." && pwd)"
source "$repo_root/scripts/common/version.sh"
source "$repo_root/scripts/common/artifacts.sh"
version_file="$repo_root/caveviewer_version.py"
app_bundle="$repo_root/dist/macos/app/CaveViewer.app"
readme_path="$repo_root/README.md"
packages_dir="$repo_root/dist/macos/packages"
metadata_dir="$repo_root/dist/macos/metadata"

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "Error: this script must be run on macOS."
  exit 1
fi

if [ ! -f "$version_file" ]; then
  echo "Error: version file not found: $version_file"
  exit 1
fi

if [ ! -d "$app_bundle" ]; then
  echo "Error: app bundle not found at $app_bundle"
  echo "Build it first with: ./scripts/macos/build_macos_app.sh"
  exit 1
fi

if [ ! -f "$readme_path" ]; then
  echo "Error: README not found at $readme_path"
  exit 1
fi

version="$(cv_read_app_version "$version_file")"
app_name="$(cv_read_app_name "$version_file")"

if [ -z "$version" ] || [ -z "$app_name" ]; then
  echo "Error: could not parse APP_NAME/APP_VERSION from $version_file"
  exit 1
fi

artifact_name="CaveViewer-${version}.dmg"
artifact_path="$packages_dir/$artifact_name"
meta_name="CaveViewer-${version}.json"
meta_path="$metadata_dir/$meta_name"

mkdir -p "$packages_dir" "$metadata_dir"
rm -f "$artifact_path" "$meta_path"

staging_dir="$(mktemp -d /tmp/caveviewer_dmg_staging.XXXXXX)"
cleanup() {
  rm -rf "$staging_dir"
}
trap cleanup EXIT

cp -R "$app_bundle" "$staging_dir/"
cp "$readme_path" "$staging_dir/README.md"
ln -s /Applications "$staging_dir/Applications"

hdiutil create \
  -volname "$app_name $version" \
  -srcfolder "$staging_dir" \
  -ov \
  -format UDZO \
  "$artifact_path" >/dev/null

sha256="$(cv_sha256 "$artifact_path")"
size_bytes="$(cv_size_bytes "$artifact_path")"
created_at_utc="$(cv_created_at_utc)"

base_download_url="${1:-}"
download_url=""
if [ -n "$base_download_url" ]; then
  base_trimmed="${base_download_url%/}"
  download_url="$base_trimmed/$artifact_name"
fi

cat > "$meta_path" <<EOF
{
  "app_name": "$app_name",
  "version": "$version",
  "artifact_file": "$artifact_name",
  "artifact_path": "dist/macos/packages/$artifact_name",
  "sha256": "$sha256",
  "size_bytes": $size_bytes,
  "created_at_utc": "$created_at_utc",
  "download_url": "$download_url"
}
EOF

# By default, remove the intermediate app bundle after DMG creation so
# releases don't leave a standalone .app artifact in dist/macos/app.
if [ "${KEEP_APP_BUNDLE:-0}" != "1" ]; then
  rm -rf "$app_bundle"
fi

echo "Packaged DMG artifact: $artifact_path"
echo "Metadata file: $meta_path"
echo "SHA256: $sha256"
