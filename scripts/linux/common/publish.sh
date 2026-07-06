#!/usr/bin/env bash
set -euo pipefail

# Builds the Linux app AppImage release artifact, publishes (or updates) a
# GitHub release, and writes an architecture-specific updates/linux/*/stable.json for the Linux AppImage
# updater flow.
#
# Usage:
#   ./scripts/linux/common/publish.sh [--skip-build] [--pre-release] <version> [release_notes]
#
# Example:
#   ./scripts/linux/common/publish.sh 1.0.2 "Bug fixes and stability improvements"
#
skip_build=false
pre_release=false

print_usage() {
  cat <<'EOF'
Usage:
  publish.sh [--skip-build] [--pre-release] <version> [release_notes]
  publish.sh --help

Internal shared publisher. Prefer:
  ./scripts/linux/arm64/publish.sh <version> "Release notes"
  ./scripts/linux/x86_64/publish.sh <version> "Release notes"
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

# Keep filename/version fields normalized while still creating tags in vX.Y.Z format.
normalized_version="${version#v}"
tag="v$normalized_version"
release_title="$tag"
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "$script_dir/../../.." && pwd)"
source "$repo_root/scripts/common/version.sh"
source "$repo_root/scripts/common/github.sh"
version_file="$repo_root/caveviewer_version.py"

collect_linux_artifacts() {
  map_appimage_paths=()
  while IFS= read -r -d '' f; do
    map_appimage_paths+=("$f")
  done < <(find "$repo_root/dist/linux" -path "*/packages/CaveViewer-${normalized_version}-*.AppImage" -print0 2>/dev/null | sort -z)
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
echo "Prerelease: $pre_release"

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
  linux_build_arch="${CAVEVIEWER_LINUX_UPDATE_ARCH:-both}"
  echo "Building Linux release artifacts in Docker for: $linux_build_arch"
  "$repo_root/scripts/linux/build_linux_in_docker.sh" --arch="$linux_build_arch" --step=all
fi

# Find all AppImages for this version regardless of architecture suffix.
# When building both arm64 and amd64, both are uploaded to the release.
collect_linux_artifacts

if [ ${#map_appimage_paths[@]} -eq 0 ]; then
  echo "Error: no Linux AppImage found under dist/linux/*/packages for version $normalized_version"
  exit 1
fi

# Prefer x86_64 for the update manifest (largest installed base); fall back to first found.
# Set CAVEVIEWER_LINUX_UPDATE_ARCH=arm64 or x86_64 to choose a specific architecture.
manifest_appimage_path="${map_appimage_paths[0]}"
linux_update_arch="${CAVEVIEWER_LINUX_UPDATE_ARCH:-}"
if [ -n "$linux_update_arch" ]; then
  case "$linux_update_arch" in
    arm64)
      linux_update_suffix="aarch64"
      linux_manifest_arch_dir="arm64"
      ;;
    amd64|x86|x86_64)
      linux_update_suffix="x86_64"
      linux_manifest_arch_dir="x86_64"
      ;;
    *)
      echo "Error: invalid CAVEVIEWER_LINUX_UPDATE_ARCH '$linux_update_arch' (expected arm64, amd64, x86, or x86_64)"
      exit 1
      ;;
  esac

  manifest_appimage_path=""
  for _p in "${map_appimage_paths[@]}"; do
    [[ "$_p" == *"$linux_update_suffix"* ]] && manifest_appimage_path="$_p" && break
  done
  if [ -z "$manifest_appimage_path" ]; then
    echo "Error: no Linux AppImage found for update architecture '$linux_update_arch' ($linux_update_suffix)"
    exit 1
  fi
else
  for _p in "${map_appimage_paths[@]}"; do
    [[ "$_p" == *x86_64* ]] && manifest_appimage_path="$_p" && break
  done
  if [[ "$manifest_appimage_path" == *aarch64* ]]; then
    linux_manifest_arch_dir="arm64"
  else
    linux_manifest_arch_dir="x86_64"
  fi
fi
manifest_appimage_name="$(basename "$manifest_appimage_path")"
update_manifest_path="$repo_root/updates/linux/$linux_manifest_arch_dir/stable.json"
update_manifest_signature_path="$update_manifest_path.sig"
upload_appimage_paths=("$manifest_appimage_path")

if gh release view "$tag" --repo "$repo" >/dev/null 2>&1; then
  echo "Release $tag already exists; uploading/replacing assets"
  gh release upload "$tag" "${upload_appimage_paths[@]}" --repo "$repo" --clobber
else
  echo "Creating release $tag and uploading Linux AppImages"
  create_args=("$tag" "${upload_appimage_paths[@]}" --repo "$repo" --title "$release_title" --notes "$release_notes")
  $pre_release && create_args+=(--prerelease)
  gh release create "${create_args[@]}"
fi

appimage_asset_url="$(gh api "repos/$repo/releases/tags/$tag" --jq ".assets[] | select(.name == \"$manifest_appimage_name\") | .browser_download_url")"

if [ -z "$appimage_asset_url" ]; then
  echo "Error: could not resolve browser_download_url for asset $app_appimage_name on release $tag"
  exit 1
fi

echo "Linux AppImage asset URL (manifest): $appimage_asset_url"
CAVEVIEWER_LINUX_UPDATE_ARCH="$linux_manifest_arch_dir" \
"$script_dir/update_manifest.sh" \
  "$normalized_version" \
  "$appimage_asset_url" \
  "$manifest_appimage_path" \
  "$release_notes"

signing_python="${CAVEVIEWER_RELEASE_SIGNING_PYTHON:-}"
if [ -z "$signing_python" ]; then
  linux_build_venv="${CAVEVIEWER_LINUX_BUILD_VENV:-$repo_root/.venv-linux-build}"
  if [ -x "$linux_build_venv/bin/python" ]; then
    signing_python="$linux_build_venv/bin/python"
  elif [ -x "$repo_root/.venv-dev/bin/python" ]; then
    signing_python="$repo_root/.venv-dev/bin/python"
  else
    signing_python="python3"
  fi
fi

echo "Signing Linux update manifest: $update_manifest_path"
"$signing_python" "$repo_root/scripts/sign_update_manifest.py" \
  "$update_manifest_path" \
  --signature "$update_manifest_signature_path"

echo "Manifest written locally: updates/linux/$linux_manifest_arch_dir/stable.json"
echo "Manifest signature written locally: updates/linux/$linux_manifest_arch_dir/stable.json.sig"
echo "Committing version bump and updated Linux $linux_manifest_arch_dir manifest..."
git -C "$repo_root" add \
  caveviewer_version.py \
  "updates/linux/$linux_manifest_arch_dir/stable.json" \
  "updates/linux/$linux_manifest_arch_dir/stable.json.sig"
git -C "$repo_root" commit -m "Release $tag Linux $linux_manifest_arch_dir"
git -C "$repo_root" push

echo "Done. Release $tag is published."
