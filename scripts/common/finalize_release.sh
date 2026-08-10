#!/usr/bin/env bash
set -euo pipefail

# Single-writer GitHub release finalizer.
#
# Platform workflows only build packages and upload workflow artifacts. This
# script runs after those independent jobs finish, verifies every requested
# artifact before changing external state, uploads the assets in one release
# operation, writes/signs the requested update manifests, and pushes one
# metadata commit. Keeping these shared writes in one process is what makes the
# expensive platform builds safe to run in parallel.

print_usage() {
  cat <<'EOF'
Usage:
  finalize_release.sh --platforms=<targets> --version=<version> --notes=<notes> \
    --artifacts-dir=<path> --target-branch=<branch> --expected-source-sha=<sha> \
    [--pre-release]
  finalize_release.sh --help

Targets:
  all, windows, linux-x86_64, macos-arm64, macos-x86_64

This is an internal CI helper. Use scripts/release.sh for local releases.
EOF
}

platforms=""
version=""
release_notes=""
artifacts_dir=""
target_branch=""
expected_source_sha=""
pre_release=false

while [ "$#" -gt 0 ]; do
  case "$1" in
    --platforms=*) platforms="${1#--platforms=}"; shift ;;
    --platforms)
      shift
      if [ "$#" -eq 0 ]; then echo "Error: --platforms requires a value."; exit 1; fi
      platforms="$1"
      shift
      ;;
    --version=*) version="${1#--version=}"; shift ;;
    --version)
      shift
      if [ "$#" -eq 0 ]; then echo "Error: --version requires a value."; exit 1; fi
      version="$1"
      shift
      ;;
    --notes=*) release_notes="${1#--notes=}"; shift ;;
    --notes)
      shift
      if [ "$#" -eq 0 ]; then echo "Error: --notes requires a value."; exit 1; fi
      release_notes="$1"
      shift
      ;;
    --artifacts-dir=*) artifacts_dir="${1#--artifacts-dir=}"; shift ;;
    --artifacts-dir)
      shift
      if [ "$#" -eq 0 ]; then echo "Error: --artifacts-dir requires a value."; exit 1; fi
      artifacts_dir="$1"
      shift
      ;;
    --target-branch=*) target_branch="${1#--target-branch=}"; shift ;;
    --target-branch)
      shift
      if [ "$#" -eq 0 ]; then echo "Error: --target-branch requires a value."; exit 1; fi
      target_branch="$1"
      shift
      ;;
    --expected-source-sha=*) expected_source_sha="${1#--expected-source-sha=}"; shift ;;
    --expected-source-sha)
      shift
      if [ "$#" -eq 0 ]; then echo "Error: --expected-source-sha requires a value."; exit 1; fi
      expected_source_sha="$1"
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
    --*)
      echo "Error: unknown option '$1'"
      print_usage
      exit 1
      ;;
    *)
      echo "Error: positional arguments are not supported: '$1'"
      print_usage
      exit 1
      ;;
  esac
done

if [ -z "$platforms" ] || [ -z "$version" ] || [ -z "$release_notes" ] \
  || [ -z "$artifacts_dir" ] || [ -z "$target_branch" ] \
  || [ -z "$expected_source_sha" ]; then
  echo "Error: --platforms, --version, --notes, --artifacts-dir, --target-branch, and --expected-source-sha are required."
  print_usage
  exit 1
fi

if [ -z "${CAVEVIEWER_RELEASE_SIGNING_PRIVATE_KEY:-}" ]; then
  echo "Error: CAVEVIEWER_RELEASE_SIGNING_PRIVATE_KEY must be set when finalizing a release."
  exit 1
fi

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "$script_dir/../.." && pwd)"
source "$script_dir/github.sh"
source "$script_dir/version.sh"

cv_require_cmd gh
cv_require_cmd git

if ! gh auth status >/dev/null 2>&1; then
  echo "Error: GitHub CLI is not authenticated."
  exit 1
fi

if [ ! -d "$artifacts_dir" ]; then
  echo "Error: artifacts directory not found: $artifacts_dir"
  exit 1
fi
artifacts_dir="$(cd "$artifacts_dir" && pwd)"

normalized_version="${version#v}"
if [ -z "$normalized_version" ]; then
  echo "Error: version cannot be empty."
  exit 1
fi
tag="v$normalized_version"
manifest_channel="stable"
$pre_release && manifest_channel="prerelease"

selected_windows=false
selected_linux_x86_64=false
selected_macos_arm64=false
selected_macos_x86_64=false

select_platform() {
  case "$1" in
    all)
      selected_windows=true
      selected_linux_x86_64=true
      selected_macos_arm64=true
      selected_macos_x86_64=true
      ;;
    windows) selected_windows=true ;;
    linux-x86_64) selected_linux_x86_64=true ;;
    macos-arm64) selected_macos_arm64=true ;;
    macos-x86_64) selected_macos_x86_64=true ;;
    *)
      echo "Error: unknown platform '$1'"
      exit 1
      ;;
  esac
}

IFS=',' read -r -a requested_platforms <<< "$platforms"
for requested_platform in "${requested_platforms[@]}"; do
  select_platform "${requested_platform// /}"
done

find_artifact() {
  local filename="$1"
  local matches=()
  while IFS= read -r -d '' match; do
    matches+=("$match")
  done < <(find "$artifacts_dir" -type f -name "$filename" -print0)

  if [ "${#matches[@]}" -ne 1 ]; then
    echo "Error: expected exactly one '$filename' under $artifacts_dir; found ${#matches[@]}." >&2
    return 1
  fi
  printf '%s\n' "${matches[0]}"
}

release_assets=()
windows_zip_path=""
linux_x86_64_path=""
macos_arm64_path=""
macos_x86_64_path=""

# Resolve every requested artifact before creating or modifying a release. A
# missing platform package therefore cannot leave a partially published set.
if $selected_windows; then
  windows_zip_path="$(find_artifact "CaveViewer-${normalized_version}-windows.zip")"
  windows_meta_path="$(find_artifact "CaveViewer-${normalized_version}.json")"
  windows_update_meta_path="$(find_artifact "CaveViewer-${normalized_version}.update.json")"
  release_assets+=("$windows_zip_path" "$windows_meta_path" "$windows_update_meta_path")
fi
if $selected_linux_x86_64; then
  linux_x86_64_path="$(find_artifact "CaveViewer-${normalized_version}-x86_64.AppImage")"
  release_assets+=("$linux_x86_64_path")
fi
if $selected_macos_arm64; then
  macos_arm64_path="$(find_artifact "CaveViewer-${normalized_version}-macos-arm64.dmg")"
  macos_arm64_meta_path="$(find_artifact "CaveViewer-${normalized_version}-macos-arm64.json")"
  release_assets+=("$macos_arm64_path" "$macos_arm64_meta_path")
fi
if $selected_macos_x86_64; then
  macos_x86_64_path="$(find_artifact "CaveViewer-${normalized_version}-macos-x86_64.dmg")"
  macos_x86_64_meta_path="$(find_artifact "CaveViewer-${normalized_version}-macos-x86_64.json")"
  release_assets+=("$macos_x86_64_path" "$macos_x86_64_meta_path")
fi

if [ "${#release_assets[@]}" -eq 0 ]; then
  echo "Error: no release platforms were selected."
  exit 1
fi

version_file="$repo_root/src/caveviewer/version.py"
if [ ! -f "$version_file" ]; then
  echo "Error: version file not found: $version_file"
  exit 1
fi
current_version="$(cv_read_app_version "$version_file")"
if [ -z "$current_version" ]; then
  echo "Error: APP_VERSION assignment not found in $version_file"
  exit 1
fi

signing_key_path="$CAVEVIEWER_RELEASE_SIGNING_PRIVATE_KEY"
if [ ! -f "$signing_key_path" ]; then
  echo "Error: release signing private key not found: $signing_key_path"
  exit 1
fi
signing_python="${CAVEVIEWER_RELEASE_SIGNING_PYTHON:-python3}"
if [ ! -x "$signing_python" ] && ! command -v "$signing_python" >/dev/null 2>&1; then
  echo "Error: manifest signing Python not found: $signing_python"
  exit 1
fi
"$signing_python" -c \
  'import sys; from pathlib import Path; from cryptography.hazmat.primitives import serialization; serialization.load_pem_private_key(Path(sys.argv[1]).read_bytes(), password=None)' \
  "$signing_key_path"

resolved_expected_sha="$(git -C "$repo_root" rev-parse "${expected_source_sha}^{commit}")"
current_sha="$(git -C "$repo_root" rev-parse HEAD)"
if [ "$current_sha" != "$resolved_expected_sha" ]; then
  echo "Error: checked-out source $current_sha does not match expected source $resolved_expected_sha."
  exit 1
fi

remote_sha="$(git -C "$repo_root" ls-remote --heads origin "refs/heads/$target_branch" | awk '{print $1}')"
if [ -z "$remote_sha" ]; then
  echo "Error: target branch not found on origin: $target_branch"
  exit 1
fi
if [ "$remote_sha" != "$resolved_expected_sha" ]; then
  echo "Error: origin/$target_branch moved from $resolved_expected_sha to $remote_sha while packages were building."
  echo "Restart the release so every artifact and metadata commit use one source revision."
  exit 1
fi

if ! git -C "$repo_root" diff --quiet || ! git -C "$repo_root" diff --cached --quiet; then
  echo "Error: tracked files changed before release finalization."
  exit 1
fi

repo="${CAVEVIEWER_GITHUB_REPO:-}"
if [ -z "$repo" ]; then
  repo="$(cv_infer_repo "$repo_root" || true)"
fi
if [ -z "$repo" ]; then
  echo "Error: could not determine repository. Set CAVEVIEWER_GITHUB_REPO=owner/repo."
  exit 1
fi

echo "Finalizing $tag from source $resolved_expected_sha"
echo "Platforms: $platforms"
echo "Channel: $manifest_channel"

manifest_git_paths=()
release_base_url="https://github.com/$repo/releases/download/$tag"

sign_manifest() {
  local manifest_path="$1"
  local relative_manifest_path="${manifest_path#"$repo_root/"}"
  local signature_path="$manifest_path.sig"
  "$signing_python" -c \
    'import json, sys; json.load(open(sys.argv[1], encoding="utf-8"))' \
    "$manifest_path"
  "$signing_python" "$repo_root/scripts/sign_update_manifest.py" \
    "$manifest_path" \
    --signature "$signature_path"
  manifest_git_paths+=("$relative_manifest_path" "$relative_manifest_path.sig")
}

if $selected_windows; then
  "$repo_root/scripts/windows/update_manifest.sh" \
    --version "$normalized_version" \
    --download-url "$release_base_url/$(basename "$windows_zip_path")" \
    --artifact-file "$windows_zip_path" \
    --notes "$release_notes" \
    --channel "$manifest_channel"
  sign_manifest "$repo_root/updates/windows/$manifest_channel.json"
fi

if $selected_linux_x86_64; then
  CAVEVIEWER_LINUX_UPDATE_ARCH=x86_64 \
  "$repo_root/scripts/linux/common/update_manifest.sh" \
    --version "$normalized_version" \
    --download-url "$release_base_url/$(basename "$linux_x86_64_path")" \
    --artifact-file "$linux_x86_64_path" \
    --notes "$release_notes" \
    --channel "$manifest_channel"
  sign_manifest "$repo_root/updates/linux/x86_64/$manifest_channel.json"
fi

if $selected_macos_arm64; then
  "$repo_root/scripts/macos/update_manifest.sh" \
    --arch arm64 \
    --version "$normalized_version" \
    --download-url "$release_base_url/$(basename "$macos_arm64_path")" \
    --artifact-file "$macos_arm64_path" \
    --notes "$release_notes" \
    --channel "$manifest_channel"
  arm64_manifest="$repo_root/updates/macos/arm64/$manifest_channel.json"
  sign_manifest "$arm64_manifest"

  # Installed pre-architecture clients still read the top-level macOS path.
  cp "$arm64_manifest" "$repo_root/updates/macos/$manifest_channel.json"
  cp "$arm64_manifest.sig" "$repo_root/updates/macos/$manifest_channel.json.sig"
  manifest_git_paths+=(
    "updates/macos/$manifest_channel.json"
    "updates/macos/$manifest_channel.json.sig"
  )
fi

if $selected_macos_x86_64; then
  "$repo_root/scripts/macos/update_manifest.sh" \
    --arch x86_64 \
    --version "$normalized_version" \
    --download-url "$release_base_url/$(basename "$macos_x86_64_path")" \
    --artifact-file "$macos_x86_64_path" \
    --notes "$release_notes" \
    --channel "$manifest_channel"
  sign_manifest "$repo_root/updates/macos/x86_64/$manifest_channel.json"
fi

if [ "$current_version" != "$normalized_version" ]; then
  cv_set_app_version "$version_file" "$normalized_version"
  echo "Set APP_VERSION: $current_version -> $normalized_version"
fi

# Only touch the GitHub release after every local artifact, manifest, signature,
# version, and branch preflight succeeds.
if gh release view "$tag" --repo "$repo" >/dev/null 2>&1; then
  echo "Release $tag already exists; uploading/replacing the requested assets."
  gh release upload "$tag" "${release_assets[@]}" --repo "$repo" --clobber
else
  echo "Creating release $tag with ${#release_assets[@]} asset(s)."
  create_args=(
    "$tag"
    "${release_assets[@]}"
    --repo "$repo"
    --target "$resolved_expected_sha"
    --title "$tag"
    --notes "$release_notes"
  )
  $pre_release && create_args+=(--prerelease)
  gh release create "${create_args[@]}"
fi

git -C "$repo_root" add \
  src/caveviewer/version.py \
  packaging/linux/io.github.caveviewer.caveviewer.metainfo.xml \
  "${manifest_git_paths[@]}"
if git -C "$repo_root" diff --cached --quiet; then
  echo "Release metadata already matches $tag; no commit is required."
else
  git -C "$repo_root" commit -m "Release $tag $manifest_channel"
fi

# An ordinary fast-forward push is the final optimistic-concurrency check. If
# the branch changed after the earlier remote-SHA check, publication stops
# rather than rebasing metadata onto source that the artifacts did not use.
git -C "$repo_root" push origin "HEAD:refs/heads/$target_branch"

echo "Release $tag is published with one signed metadata update."
