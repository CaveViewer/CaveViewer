#!/usr/bin/env bash
set -euo pipefail

# Builds the Windows zip release artifact, publishes (or updates) a
# GitHub release, and writes updates/windows/stable.json for the Windows updater flow.
#
# Usage:
#   ./scripts/windows/publish.sh [--skip-build] [--pre-release] <version> [release_notes]
#
# Example:
#   ./scripts/windows/publish.sh 1.0.2 "Bug fixes and stability improvements"
#
skip_build=false
pre_release=false

print_usage() {
  cat <<'EOF'
Usage:
  publish.sh [--skip-build] [--pre-release] <version> [release_notes]
  publish.sh --help

Example:
  publish.sh 1.0.2 "Bug fixes and stability improvements"
EOF
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --skip-build)
      skip_build=true
      shift
      ;;
    --pre-release)
      pre_release=true
      shift
      ;;
    -h|--help)
      print_usage
      exit 0
      ;;
    --)
      shift
      break
      ;;
    -*)
      echo "Error: unknown option '$1'"
      echo ""
      print_usage
      exit 1
      ;;
    *)
      break
      ;;
  esac
done

if [ "$#" -gt 0 ] && [ "$1" = "-h" -o "$1" = "--help" ]; then
  print_usage
  exit 0
fi

if [ "$#" -lt 1 ]; then
  echo "Error: version is required."
  echo ""
  print_usage
  exit 1
fi

version="$1"
release_notes="${2:-Release $version}"

normalized_version="${version#v}"
tag="v$normalized_version"
release_title="$tag"

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "$script_dir/../.." && pwd)"
source "$repo_root/scripts/common/version.sh"
source "$repo_root/scripts/common/github.sh"

version_file="$repo_root/caveviewer_version.py"
windows_packages_dir="$repo_root/dist/windows/packages"
windows_metadata_dir="$repo_root/dist/windows/metadata"

cv_require_cmd gh
cv_require_cmd git

if ! gh auth status >/dev/null 2>&1; then
  echo "Error: GitHub CLI is not authenticated. Run: gh auth login"
  exit 1
fi

repo="$(cv_infer_repo "$repo_root" || true)"

if [ -z "$repo" ]; then
  echo "Error: could not determine repository from the local git remote."
  exit 1
fi

echo "Using repository: $repo"
echo "Version: $normalized_version"
echo "Tag: $tag"
echo "Prerelease: $pre_release"

if [ ! -f "$version_file" ]; then
  echo "Error: version file not found: $version_file"
  exit 1
fi

if ! grep -q '^APP_VERSION = "' "$version_file"; then
  echo "Error: APP_VERSION assignment not found in $version_file"
  exit 1
fi

app_zip_name="CaveViewer-${normalized_version}-windows.zip"
app_meta_name="CaveViewer-${normalized_version}.json"
app_update_meta_name="CaveViewer-${normalized_version}.update.json"
app_zip_path="$windows_packages_dir/$app_zip_name"
app_meta_path="$windows_metadata_dir/$app_meta_name"
app_update_meta_path="$windows_metadata_dir/$app_update_meta_name"

current_version="$(cv_read_app_version "$version_file")"
if [ "$current_version" != "$normalized_version" ]; then
  cv_set_app_version "$version_file" "$normalized_version"
  echo "Bumped APP_VERSION: $current_version -> $normalized_version"
else
  echo "APP_VERSION already at $normalized_version"
fi

if $skip_build; then
  echo "Skipping package step (--skip-build)."
else
  "$script_dir/package.sh" "https://github.com/$repo/releases/download/$tag"
fi

if [ ! -f "$app_zip_path" ]; then
  echo "Error: expected Windows zip package not found: $app_zip_path"
  exit 1
fi

if [ ! -f "$app_meta_path" ]; then
  echo "Error: expected Windows metadata JSON not found: $app_meta_path"
  exit 1
fi

if [ ! -f "$app_update_meta_path" ]; then
  echo "Error: expected Windows update metadata JSON not found: $app_update_meta_path"
  exit 1
fi

if gh release view "$tag" --repo "$repo" >/dev/null 2>&1; then
  echo "Release $tag already exists; uploading/replacing assets"
  gh release upload "$tag" "$app_zip_path" "$app_meta_path" "$app_update_meta_path" --repo "$repo" --clobber
else
  echo "Creating release $tag and uploading Windows assets"
  create_args=("$tag" "$app_zip_path" "$app_meta_path" "$app_update_meta_path" --repo "$repo" --title "$release_title" --notes "$release_notes")
  $pre_release && create_args+=(--prerelease)
  gh release create "${create_args[@]}"
fi

zip_asset_url="$(gh api "repos/$repo/releases/tags/$tag" --jq ".assets[] | select(.name == \"$app_zip_name\") | .browser_download_url")"

if [ -z "$zip_asset_url" ]; then
  echo "Error: could not resolve browser_download_url for asset $app_zip_name on release $tag"
  exit 1
fi

echo "Windows zip asset URL: $zip_asset_url"

"$script_dir/update_manifest.sh" \
  "$normalized_version" \
  "$zip_asset_url" \
  "$app_zip_path" \
  "$release_notes"

echo "Committing version bump and updated manifest..."
git -C "$repo_root" add caveviewer_version.py updates/windows/stable.json
git -C "$repo_root" commit -m "Release $tag"
git -C "$repo_root" push

echo "Done. Release $tag is published and manifest is live."
