#!/usr/bin/env bash
set -euo pipefail

# Builds the macOS app DMG release artifact, publishes (or updates) a
# GitHub release, and writes updates/macos/stable.json for the macOS DMG
# updater flow.
#
# Usage:
#   ./scripts/macos/publish.sh [--skip-build] <version> [release_notes]
#
# Example:
#   ./scripts/macos/publish.sh 1.0.2 "Bug fixes and stability improvements"
#
skip_build=false

while [ "$#" -gt 0 ]; do
  case "$1" in
    --skip-build)
      skip_build=true
      shift
      ;;
    -h|--help)
      echo "Usage: $0 [--skip-build] <version> [release_notes]"
      echo "Example: $0 1.0.2 \"Bug fixes and stability improvements\""
      exit 0
      ;;
    --)
      shift
      break
      ;;
    -*)
      echo "Error: unknown option '$1'"
      exit 1
      ;;
    *)
      break
      ;;
  esac
done

if [ "$#" -gt 0 ] && [ "$1" = "-h" -o "$1" = "--help" ]; then
  echo "Usage: $0 [--skip-build] <version> [release_notes]"
  echo "Example: $0 1.0.2 \"Bug fixes and stability improvements\""
  exit 0
fi

if [ "$#" -lt 1 ]; then
  echo "Usage: $0 <version> [release_notes]"
  echo "Example: $0 1.0.2 \"Bug fixes and stability improvements\""
  exit 1
fi

version="$1"
release_notes="${2:-Release $version}"

# Keep filename/version fields normalized while still creating tags in vX.Y.Z format.
normalized_version="${version#v}"
tag="v$normalized_version"
release_title="$tag"
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "$script_dir/../.." && pwd)"
source "$repo_root/scripts/common/version.sh"
source "$repo_root/scripts/common/github.sh"
version_file="$repo_root/caveviewer_version.py"
macos_packages_dir="$repo_root/dist/macos/packages"
macos_metadata_dir="$repo_root/dist/macos/metadata"
update_manifest_path="$repo_root/updates/macos/stable.json"
update_manifest_signature_path="$update_manifest_path.sig"

cv_require_cmd gh
cv_require_cmd git

if ! gh auth status >/dev/null 2>&1; then
  echo "Error: GitHub CLI is not authenticated. Run: gh auth login"
  exit 1
fi

repo="${CAVEVIEWER_GITHUB_REPO:-}"
if [ -z "$repo" ]; then
  repo="$(cv_infer_repo "$repo_root" || true)"
fi

if [ -z "$repo" ]; then
  echo "Error: could not determine repository. Set CAVEVIEWER_GITHUB_REPO=owner/repo"
  exit 1
fi

echo "Using repository: $repo"
echo "Version: $normalized_version"
echo "Tag: $tag"

if [ ! -f "$version_file" ]; then
  echo "Error: version file not found: $version_file"
  exit 1
fi

if ! grep -q '^APP_VERSION = "' "$version_file"; then
  echo "Error: APP_VERSION assignment not found in $version_file"
  exit 1
fi

app_dmg_name="CaveViewer-${normalized_version}.dmg"
app_meta_name="CaveViewer-${normalized_version}.json"
app_dmg_path="$macos_packages_dir/$app_dmg_name"
app_meta_path="$macos_metadata_dir/$app_meta_name"

current_version="$(cv_read_app_version "$version_file")"
if [ "$current_version" != "$normalized_version" ]; then
  cv_set_app_version "$version_file" "$normalized_version"
  echo "Bumped APP_VERSION: $current_version -> $normalized_version"
else
  echo "APP_VERSION already at $normalized_version"
fi

if $skip_build; then
  echo "Skipping build/package step (--skip-build)."
else
  "$script_dir/build.sh"
  "$script_dir/package_macos_dmg.sh" "https://github.com/$repo/releases/download/$tag"
fi

if [ ! -f "$app_dmg_path" ]; then
  echo "Error: expected macOS DMG package not found: $app_dmg_path"
  exit 1
fi

if [ ! -f "$app_meta_path" ]; then
  echo "Error: expected macOS metadata JSON not found: $app_meta_path"
  exit 1
fi

if gh release view "$tag" --repo "$repo" >/dev/null 2>&1; then
  echo "Release $tag already exists; uploading/replacing assets"
  gh release upload "$tag" "$app_dmg_path" "$app_meta_path" --repo "$repo" --clobber
else
  echo "Creating release $tag and uploading macOS DMG assets"
  gh release create "$tag" "$app_dmg_path" "$app_meta_path" --repo "$repo" --title "$release_title" --notes "$release_notes"
fi

dmg_asset_url="$(gh api "repos/$repo/releases/tags/$tag" --jq ".assets[] | select(.name == \"$app_dmg_name\") | .browser_download_url")"
meta_asset_url="$(gh api "repos/$repo/releases/tags/$tag" --jq ".assets[] | select(.name == \"$app_meta_name\") | .browser_download_url")"

if [ -z "$dmg_asset_url" ]; then
  echo "Error: could not resolve browser_download_url for asset $app_dmg_name on release $tag"
  exit 1
fi

if [ -z "$meta_asset_url" ]; then
  echo "Error: could not resolve browser_download_url for asset $app_meta_name on release $tag"
  exit 1
fi

echo "macOS DMG asset URL: $dmg_asset_url"
echo "macOS metadata URL: $meta_asset_url"
"$script_dir/update_manifest.sh" \
  "$normalized_version" \
  "$dmg_asset_url" \
  "$app_dmg_path" \
  "$release_notes"

signing_python="${CAVEVIEWER_RELEASE_SIGNING_PYTHON:-}"
if [ -z "$signing_python" ]; then
  macos_build_venv="${CAVEVIEWER_MACOS_BUILD_VENV:-$repo_root/.venv-macos-build}"
  if [ -x "$macos_build_venv/bin/python" ]; then
    signing_python="$macos_build_venv/bin/python"
  elif [ -x "$repo_root/.venv-dev/bin/python" ]; then
    signing_python="$repo_root/.venv-dev/bin/python"
  else
    signing_python="python3"
  fi
fi

echo "Signing macOS update manifest: $update_manifest_path"
"$signing_python" "$repo_root/scripts/sign_update_manifest.py" \
  "$update_manifest_path" \
  --signature "$update_manifest_signature_path"

echo "Committing version bump and updated manifest..."
git -C "$repo_root" add caveviewer_version.py updates/macos/stable.json updates/macos/stable.json.sig
git -C "$repo_root" commit -m "Release $tag"
git -C "$repo_root" push

echo "Done. Release $tag is published and manifest is live."
