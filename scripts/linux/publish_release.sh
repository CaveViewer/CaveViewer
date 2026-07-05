#!/usr/bin/env bash
set -euo pipefail

# Builds the Linux app AppImage release artifact, publishes (or updates) a
# GitHub release, and writes updates/linux/stable.json for the Linux AppImage
# updater flow.
#
# Usage:
#   ./scripts/linux/publish_release.sh [--skip-build] <version> [release_notes]
#
# Example:
#   ./scripts/linux/publish_release.sh 1.0.2 "Bug fixes and stability improvements"
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
linux_packages_dir="$repo_root/dist/linux/packages"

collect_linux_artifacts() {
  map_appimage_paths=()
  while IFS= read -r -d '' f; do
    map_appimage_paths+=("$f")
  done < <(find "$linux_packages_dir" -maxdepth 1 -name "CaveViewer-${normalized_version}-*.AppImage" -print0 2>/dev/null | sort -z)
}

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
  # Build only on Linux. On macOS, assume Docker build was already done.
  if [[ "$OSTYPE" != "darwin"* ]]; then
    "$script_dir/build_linux_app.sh"
    "$script_dir/package.sh"
  else
    echo "[skip] Build on macOS (use: ./scripts/linux/build_linux_in_docker.sh)"
  fi
fi

# Find all AppImages for this version regardless of architecture suffix.
# When building both arm64 and amd64, both are uploaded to the release.
collect_linux_artifacts

if [ ${#map_appimage_paths[@]} -eq 0 ]; then
  echo "Error: no AppImage found in $linux_packages_dir for version $normalized_version"
  exit 1
fi

# Prefer x86_64 for the update manifest (largest installed base); fall back to first found.
manifest_appimage_path="${map_appimage_paths[0]}"
for _p in "${map_appimage_paths[@]}"; do
  [[ "$_p" == *x86_64* ]] && manifest_appimage_path="$_p" && break
done
manifest_appimage_name="$(basename "$manifest_appimage_path")"

if gh release view "$tag" --repo "$repo" >/dev/null 2>&1; then
  echo "Release $tag already exists; uploading/replacing assets"
  gh release upload "$tag" "${map_appimage_paths[@]}" --repo "$repo" --clobber
else
  echo "Creating release $tag and uploading Linux AppImages"
  gh release create "$tag" "${map_appimage_paths[@]}" --repo "$repo" --title "$release_title" --notes "$release_notes"
fi

appimage_asset_url="$(gh api "repos/$repo/releases/tags/$tag" --jq ".assets[] | select(.name == \"$manifest_appimage_name\") | .browser_download_url")"

if [ -z "$appimage_asset_url" ]; then
  echo "Error: could not resolve browser_download_url for asset $app_appimage_name on release $tag"
  exit 1
fi

echo "Linux AppImage asset URL (manifest): $appimage_asset_url"
"$script_dir/write_update_manifest.sh" \
  "$normalized_version" \
  "$appimage_asset_url" \
  "$manifest_appimage_path" \
  "$release_notes"

echo "Committing version bump and updated manifest..."
git -C "$repo_root" add caveviewer_version.py updates/linux/stable.json
git -C "$repo_root" commit -m "Release $tag"
git -C "$repo_root" push

echo "Done. Release $tag is published and manifest is live."
