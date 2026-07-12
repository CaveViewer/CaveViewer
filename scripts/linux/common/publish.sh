#!/usr/bin/env bash
if [ "${BASH##*/}" != "bash" ]; then
  echo "Error: publish.sh must be run with bash, not sh."
  echo "Use: x86_64/publish.sh ..."
  exit 1
fi

set -euo pipefail

# Shared Linux publisher.
# Builds or reuses Linux AppImage artifacts, publishes/uploads a GitHub release,
# and writes a signed architecture-specific update manifest for the selected
# channel: updates/linux/<arch>/<stable|prerelease>.json.
#
# Usage:
#   publish.sh --version=<version> [--notes=<release_notes>] [--use-existing-artifacts] [--rebuild] [--pre-release]
#
# Example:
#   publish.sh --version=1.0.2 --notes="Bug fixes and stability improvements"
#
use_existing_artifacts=false
rebuild=false
pre_release=false

print_usage() {
  cat <<'EOF'
Usage:
  publish.sh --version=<version> [--notes=<release_notes>] [--use-existing-artifacts] [--rebuild] [--pre-release]
  publish.sh --help

Options:
  --version=<version>       Release version, for example 1.0.2
  --notes=<notes>           Release notes (default: "Release <version>")
  --use-existing-artifacts  Publish existing artifacts without rebuilding
  --rebuild                 Rebuild the Linux Docker image before building artifacts
  --pre-release             Mark the GitHub release as a prerelease and write prerelease.json

Internal shared publisher. Prefer:
  x86_64/publish.sh --version=<version> --notes="Release notes"
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
    --rebuild)
      rebuild=true
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
  echo "Error: CAVEVIEWER_RELEASE_SIGNING_PRIVATE_KEY must be set when publishing signed Linux update manifests."
  exit 1
fi

# Keep filename/version fields normalized while still creating tags in vX.Y.Z format.
normalized_version="${version#v}"
tag="v$normalized_version"
release_title="$tag"
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "$script_dir/../../.." && pwd)"
source "$repo_root/scripts/common/version.sh"
source "$repo_root/scripts/common/github.sh"
version_file="$repo_root/src/caveviewer/version.py"

collect_linux_artifacts() {
  map_appimage_paths=()
  while IFS= read -r -d '' f; do
    map_appimage_paths+=("$f")
  done < <(find "$repo_root/dist/linux" -path "*/packages/CaveViewer-${normalized_version}-x86_64.AppImage" -print0 2>/dev/null | sort -z)
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

if $use_existing_artifacts; then
  echo "Using existing build/package artifacts (--use-existing-artifacts)."
else
  linux_build_arch="${CAVEVIEWER_LINUX_UPDATE_ARCH:-x86_64}"
  echo "Building Linux release artifacts in Docker for: $linux_build_arch"
  build_args=(--arch="$linux_build_arch" --step=all)
  $rebuild && build_args+=(--rebuild)
  "$repo_root/scripts/linux/build_linux_in_docker.sh" "${build_args[@]}"
fi

# Find Linux x86_64 AppImages for this version.
collect_linux_artifacts

if [ ${#map_appimage_paths[@]} -eq 0 ]; then
  echo "Error: no Linux AppImage found under dist/linux/*/packages for version $normalized_version"
  exit 1
fi

# Set CAVEVIEWER_LINUX_UPDATE_ARCH=x86_64 to choose the Linux update architecture.
manifest_appimage_path="${map_appimage_paths[0]}"
linux_update_arch="${CAVEVIEWER_LINUX_UPDATE_ARCH:-}"
if [ -n "$linux_update_arch" ]; then
  case "$linux_update_arch" in
    amd64|x86|x86_64)
      linux_update_suffix="x86_64"
      linux_manifest_arch_dir="x86_64"
      ;;
    *)
      echo "Error: invalid CAVEVIEWER_LINUX_UPDATE_ARCH '$linux_update_arch' (expected x86_64)"
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
  linux_manifest_arch_dir="x86_64"
fi
manifest_appimage_name="$(basename "$manifest_appimage_path")"
manifest_channel="stable"
$pre_release && manifest_channel="prerelease"
update_manifest_path="$repo_root/updates/linux/$linux_manifest_arch_dir/$manifest_channel.json"
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
  --version "$normalized_version" \
  --download-url "$appimage_asset_url" \
  --artifact-file "$manifest_appimage_path" \
  --notes "$release_notes" \
  --channel "$manifest_channel"

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

echo "Manifest written locally: updates/linux/$linux_manifest_arch_dir/$manifest_channel.json"
echo "Manifest signature written locally: updates/linux/$linux_manifest_arch_dir/$manifest_channel.json.sig"
echo "Committing version bump and updated Linux $linux_manifest_arch_dir $manifest_channel manifest..."
git -C "$repo_root" add \
  src/caveviewer/version.py \
  packaging/linux/io.github.kernalpanic.caveviewer.metainfo.xml \
  "updates/linux/$linux_manifest_arch_dir/$manifest_channel.json" \
  "updates/linux/$linux_manifest_arch_dir/$manifest_channel.json.sig"
git -C "$repo_root" commit -m "Release $tag Linux $linux_manifest_arch_dir $manifest_channel"
git -C "$repo_root" push

echo "Done. Release $tag is published."
