#!/usr/bin/env bash
set -euo pipefail

# Builds the macOS app DMG release artifact, publishes (or updates) a
# GitHub release, and writes updates/macos/<channel>.json for the updater flow.
#
# Usage:
#   ./scripts/macos/publish.sh --version=<version> [--notes=<release_notes>] [--use-existing-artifacts] [--pre-release]
#
# Example:
#   ./scripts/macos/publish.sh --version=1.0.2 --notes="Bug fixes and stability improvements"
#
use_existing_artifacts=false
pre_release=false

print_usage() {
  cat <<'EOF'
Usage:
  publish.sh --version=<version> [--notes=<release_notes>] [--use-existing-artifacts] [--pre-release]
  publish.sh --help

Options:
  --version=<version>      Release version, for example 1.0.2
  --notes=<notes>          Release notes (default: "Release <version>")
  --use-existing-artifacts  Publish existing artifacts without rebuilding
  --pre-release             Mark the GitHub release as a prerelease and write prerelease.json

Example:
  publish.sh --version=1.0.2 --notes="Bug fixes and stability improvements"
EOF
}

version=""
release_notes=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --version=*)
      version="${1#--version=}"
      shift
      ;;
    --version)
      shift
      if [ "$#" -eq 0 ]; then
        echo "Error: --version requires a value."
        exit 1
      fi
      version="$1"
      shift
      ;;
    --notes=*)
      release_notes="${1#--notes=}"
      shift
      ;;
    --notes)
      shift
      if [ "$#" -eq 0 ]; then
        echo "Error: --notes requires a value."
        exit 1
      fi
      release_notes="$1"
      shift
      ;;
    --use-existing-artifacts)
      use_existing_artifacts=true
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
      echo "Error: positional arguments are not supported: '$1'"
      echo "Use --version=<version> and --notes=<release_notes>."
      exit 1
      ;;
  esac
done

if [ "$#" -gt 0 ] && [ "$1" = "-h" -o "$1" = "--help" ]; then
  print_usage
  exit 0
fi

if [ -z "$version" ]; then
  echo "Error: --version is required."
  echo ""
  print_usage
  exit 1
fi

if [ -z "$release_notes" ]; then
  release_notes="Release $version"
fi

if [ -z "${CAVEVIEWER_RELEASE_SIGNING_PRIVATE_KEY:-}" ]; then
  echo "Error: CAVEVIEWER_RELEASE_SIGNING_PRIVATE_KEY must be set when publishing signed macOS update manifests."
  exit 1
fi

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
manifest_channel="stable"
$pre_release && manifest_channel="prerelease"
update_manifest_path="$repo_root/updates/macos/$manifest_channel.json"
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
echo "Prerelease: $pre_release"

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

if $use_existing_artifacts; then
  echo "Using existing build/package artifacts (--use-existing-artifacts)."
else
  "$script_dir/build.sh"
  "$script_dir/package_macos_dmg.sh" --base-download-url "https://github.com/$repo/releases/download/$tag"
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
  create_args=("$tag" "$app_dmg_path" "$app_meta_path" --repo "$repo" --title "$release_title" --notes "$release_notes")
  $pre_release && create_args+=(--prerelease)
  gh release create "${create_args[@]}"
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
  --version "$normalized_version" \
  --download-url "$dmg_asset_url" \
  --artifact-file "$app_dmg_path" \
  --notes "$release_notes" \
  --channel "$manifest_channel"

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

echo "Signing macOS $manifest_channel update manifest: $update_manifest_path"
"$signing_python" "$repo_root/scripts/sign_update_manifest.py" \
  "$update_manifest_path" \
  --signature "$update_manifest_signature_path"

echo "Committing version bump and updated $manifest_channel manifest..."
git -C "$repo_root" add caveviewer_version.py "updates/macos/$manifest_channel.json" "updates/macos/$manifest_channel.json.sig"
git -C "$repo_root" commit -m "Release $tag macOS $manifest_channel"
git -C "$repo_root" push

echo "Done. Release $tag is published and $manifest_channel manifest is live."
