#!/usr/bin/env bash
set -euo pipefail

# Single-writer GitHub release finalizer.
#
# Platform workflows only build packages and upload workflow artifacts. This
# script runs after those independent jobs finish, validates every requested
# artifact locally, uploads the assets in one release operation, verifies the
# uploaded bytes through GitHub's release API, then writes/signs the requested
# update manifests and pushes one metadata commit. Keeping these shared writes
# in one process is what makes the expensive platform builds safe to run in
# parallel.

print_usage() {
  cat <<'EOF'
Usage:
  finalize_release.sh --platforms=<targets> --version=<version> --notes=<notes> \
    --artifacts-dir=<path> --target-branch=<branch> --expected-source-sha=<sha> \
    [--preview] [--allow-unsigned-windows-community]
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
preview=false
allow_unsigned_windows_community=false

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
    --preview)
      preview=true
      shift
      ;;
    --allow-unsigned-windows-community)
      allow_unsigned_windows_community=true
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
$preview && manifest_channel="preview"

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

if $allow_unsigned_windows_community && ! $selected_windows; then
  echo "Error: --allow-unsigned-windows-community requires the windows platform."
  exit 1
fi

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
windows_exe_path=""
linux_x86_64_path=""
linux_x86_64_meta_path=""
macos_arm64_path=""
macos_arm64_meta_path=""
macos_x86_64_path=""
macos_x86_64_meta_path=""

# Resolve every requested artifact before creating or modifying a release. A
# missing platform package therefore cannot leave a partially published set.
if $selected_windows; then
  windows_exe_path="$(find_artifact "CaveViewer-${normalized_version}-windows.exe")"
  windows_meta_path="$(find_artifact "CaveViewer-${normalized_version}.json")"
  windows_update_meta_path="$(find_artifact "CaveViewer-${normalized_version}.update.json")"
  release_assets+=("$windows_exe_path" "$windows_meta_path" "$windows_update_meta_path")
fi
if $selected_linux_x86_64; then
  linux_x86_64_path="$(find_artifact "CaveViewer-${normalized_version}-x86_64.AppImage")"
  linux_x86_64_meta_path="$(find_artifact "CaveViewer-${normalized_version}-linux-x86_64.json")"
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

release_channel_verifier="$repo_root/scripts/common/verify_release_channel.py"
if [ ! -f "$release_channel_verifier" ]; then
  echo "Error: package release-channel verifier is missing: $release_channel_verifier"
  exit 1
fi
verify_package_release_channel() {
  "$signing_python" "$release_channel_verifier" \
    --metadata-file "$1" \
    --expected-release-channel "$manifest_channel"
}

if $selected_windows; then
  windows_metadata_verify_args=(
    --artifact-file "$windows_exe_path" \
    --metadata-file "$windows_meta_path" \
    --update-metadata-file "$windows_update_meta_path" \
    --release-channel "$manifest_channel"
  )
  if $allow_unsigned_windows_community; then
    windows_metadata_verify_args+=(--allow-unsigned-community)
  fi
  "$signing_python" "$repo_root/scripts/windows/verify_package_metadata.py" \
    "${windows_metadata_verify_args[@]}"
  windows_authenticode_status="$(
    "$signing_python" -c \
      'import json, sys; value = json.load(open(sys.argv[1], encoding="utf-8")).get("authenticode_status"); isinstance(value, str) and value.strip() or sys.exit("Error: Windows update metadata has no Authenticode status."); print(value.strip())' \
      "$windows_update_meta_path"
  )"
  windows_authenticode_certificate_subject=""
  if ! $allow_unsigned_windows_community; then
    windows_authenticode_certificate_subject="$(
      "$signing_python" -c \
        'import json, sys; value = json.load(open(sys.argv[1], encoding="utf-8")).get("authenticode_certificate_subject"); isinstance(value, str) and value.strip() or sys.exit("Error: Windows update metadata has no Authenticode certificate subject."); print(value.strip())' \
        "$windows_update_meta_path"
    )"
  fi
fi
if $selected_linux_x86_64; then
  verify_package_release_channel "$linux_x86_64_meta_path"
fi
if $selected_macos_arm64; then
  verify_package_release_channel "$macos_arm64_meta_path"
fi
if $selected_macos_x86_64; then
  verify_package_release_channel "$macos_x86_64_meta_path"
fi

ensure_target_branch_at_expected_source() {
  local observed_remote_sha
  observed_remote_sha="$(
    git -C "$repo_root" ls-remote --heads origin "refs/heads/$target_branch" |
      awk '{print $1}'
  )"
  if [ -z "$observed_remote_sha" ]; then
    echo "Error: target branch not found on origin: $target_branch"
    exit 1
  fi
  if [ "$observed_remote_sha" != "$resolved_expected_sha" ]; then
    echo "Error: origin/$target_branch moved from $resolved_expected_sha to $observed_remote_sha while packages were building."
    echo "Restart the release so every artifact and metadata commit use one source revision."
    exit 1
  fi
}

ensure_release_metadata_is_reconciled() {
  if [ "$target_branch" = "main" ]; then
    return
  fi

  git -C "$repo_root" fetch --no-tags origin \
    "refs/heads/main:refs/remotes/origin/main"
  if git -C "$repo_root" diff --quiet "origin/main" "$resolved_expected_sha" -- \
    src/caveviewer/version.py \
    packaging/linux/io.github.caveviewer.caveviewer.metainfo.xml \
    updates; then
    return
  fi

  # A same-version partial platform publish can be resumed. Any different
  # version or more than one unmerged AppStream record must reach main first.
  local unmerged_release_versions
  unmerged_release_versions="$(
    git -C "$repo_root" diff --unified=0 "origin/main" "$resolved_expected_sha" -- \
      packaging/linux/io.github.caveviewer.caveviewer.metainfo.xml |
      awk -F '"' '/^\+.*<release version="/ { print $2 }'
  )"
  if [ "$current_version" = "$normalized_version" ] &&
    [ "$unmerged_release_versions" = "$normalized_version" ]; then
    return
  fi

  echo "Error: target branch '$target_branch' has release metadata not reconciled with origin/main."
  echo "Merge the existing release metadata into main, then rebase the target branch before publishing another release."
  exit 1
}

resolved_expected_sha="$(git -C "$repo_root" rev-parse "${expected_source_sha}^{commit}")"
current_sha="$(git -C "$repo_root" rev-parse HEAD)"
if [ "$current_sha" != "$resolved_expected_sha" ]; then
  echo "Error: checked-out source $current_sha does not match expected source $resolved_expected_sha."
  exit 1
fi
ensure_target_branch_at_expected_source
ensure_release_metadata_is_reconciled

ensure_stable_version_exceeds_preview() {
  $preview && return

  local preview_manifests=()
  while IFS= read -r manifest; do
    preview_manifests+=("$manifest")
  done < <(find "$repo_root/updates" -type f -name preview.json -print | sort)
  [ "${#preview_manifests[@]}" -eq 0 ] && return

  "$signing_python" - "$normalized_version" "${preview_manifests[@]}" <<'PY'
import json
import sys
from pathlib import Path


def version_tuple(value: str) -> tuple[int, ...]:
    parts = value.split(".")
    if not parts or any(not part.isdecimal() for part in parts):
        raise SystemExit(f"Error: invalid numeric release version: {value!r}")
    return tuple(int(part) for part in parts)


def is_greater(candidate: tuple[int, ...], baseline: tuple[int, ...]) -> bool:
    width = max(len(candidate), len(baseline))
    return candidate + (0,) * (width - len(candidate)) > baseline + (0,) * (
        width - len(baseline)
    )


stable_version = sys.argv[1]
stable_tuple = version_tuple(stable_version)
for raw_path in sys.argv[2:]:
    path = Path(raw_path)
    preview_version = str(
        json.loads(path.read_text(encoding="utf-8")).get("latest_version", "")
    ).strip()
    if not is_greater(stable_tuple, version_tuple(preview_version)):
        raise SystemExit(
            "Error: stable release version "
            f"{stable_version} must be greater than Preview {preview_version} "
            f"advertised by {path}."
        )
PY
}

ensure_stable_version_exceeds_preview

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

# Only touch the GitHub release after every local artifact, package metadata,
# signing-key, source-revision, and branch preflight succeeds. Update manifests
# remain unchanged until the uploaded assets are independently verified below.
if gh release view "$tag" --repo "$repo" >/dev/null 2>&1; then
  existing_is_preview="$(
    gh release view "$tag" --repo "$repo" --json isPrerelease --jq .isPrerelease
  )"
  requested_is_preview=false
  $preview && requested_is_preview=true
  if [ "$existing_is_preview" != "$requested_is_preview" ]; then
    echo "Error: release tag $tag already belongs to the other update channel." >&2
    echo "Use a new numeric version; Stable and Preview may not share a tag." >&2
    exit 1
  fi
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
  $preview && create_args+=(--prerelease)
  gh release create "${create_args[@]}"
fi

release_asset_verifier="$repo_root/scripts/common/verify_release_asset.py"
if [ ! -f "$release_asset_verifier" ]; then
  echo "Error: release asset verifier is missing: $release_asset_verifier"
  exit 1
fi
release_json_path="$(mktemp)"
trap 'rm -f "$release_json_path"' EXIT
gh api "repos/$repo/releases/tags/$tag" > "$release_json_path"

# macOS ships Bash 3.2, which supports the indexed release-assets array but not
# associative arrays. Keep only the verified package URLs needed by manifests
# in explicit scalars while still verifying every uploaded release asset first.
verified_release_url=""
windows_exe_release_url=""
linux_x86_64_release_url=""
macos_arm64_release_url=""
macos_x86_64_release_url=""
for release_asset in "${release_assets[@]}"; do
  verified_release_url="$(
    "$signing_python" "$release_asset_verifier" \
      --release-json "$release_json_path" \
      --artifact "$release_asset" \
      --expected-tag "$tag"
  )"
  if [ "$release_asset" = "$windows_exe_path" ]; then
    windows_exe_release_url="$verified_release_url"
  elif [ "$release_asset" = "$linux_x86_64_path" ]; then
    linux_x86_64_release_url="$verified_release_url"
  elif [ "$release_asset" = "$macos_arm64_path" ]; then
    macos_arm64_release_url="$verified_release_url"
  elif [ "$release_asset" = "$macos_x86_64_path" ]; then
    macos_x86_64_release_url="$verified_release_url"
  fi
done
echo "Verified ${#release_assets[@]} uploaded release asset(s) against GitHub metadata."

manifest_git_paths=()

add_legacy_preview_alias() {
  $preview || return 0

  local manifest_path="$1"
  local legacy_manifest_path="${manifest_path%/preview.json}/prerelease.json"
  cp "$manifest_path" "$legacy_manifest_path"
  cp "$manifest_path.sig" "$legacy_manifest_path.sig"
  manifest_git_paths+=(
    "${legacy_manifest_path#"$repo_root/"}"
    "${legacy_manifest_path#"$repo_root/"}.sig"
  )
}

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
  add_legacy_preview_alias "$manifest_path"
}

if $selected_windows; then
  windows_manifest_args=(
    --version "$normalized_version" \
    --download-url "$windows_exe_release_url" \
    --artifact-file "$windows_exe_path" \
    --notes "$release_notes" \
    --channel "$manifest_channel" \
    --authenticode-status "$windows_authenticode_status"
  )
  if [ -n "$windows_authenticode_certificate_subject" ]; then
    windows_manifest_args+=(
      --authenticode-certificate-subject "$windows_authenticode_certificate_subject"
    )
  fi
  "$repo_root/scripts/windows/update_manifest.sh" \
    "${windows_manifest_args[@]}"
  sign_manifest "$repo_root/updates/windows/$manifest_channel.json"
fi

if $selected_linux_x86_64; then
  CAVEVIEWER_LINUX_UPDATE_ARCH=x86_64 \
  "$repo_root/scripts/linux/common/update_manifest.sh" \
    --version "$normalized_version" \
    --download-url "$linux_x86_64_release_url" \
    --artifact-file "$linux_x86_64_path" \
    --notes "$release_notes" \
    --channel "$manifest_channel"
  sign_manifest "$repo_root/updates/linux/x86_64/$manifest_channel.json"
fi

if $selected_macos_arm64; then
  "$repo_root/scripts/macos/update_manifest.sh" \
    --arch arm64 \
    --version "$normalized_version" \
    --download-url "$macos_arm64_release_url" \
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
  add_legacy_preview_alias "$repo_root/updates/macos/$manifest_channel.json"
fi

if $selected_macos_x86_64; then
  "$repo_root/scripts/macos/update_manifest.sh" \
    --arch x86_64 \
    --version "$normalized_version" \
    --download-url "$macos_x86_64_release_url" \
    --artifact-file "$macos_x86_64_path" \
    --notes "$release_notes" \
    --channel "$manifest_channel"
  sign_manifest "$repo_root/updates/macos/x86_64/$manifest_channel.json"
fi

if [ "$current_version" != "$normalized_version" ]; then
  cv_set_app_version "$version_file" "$normalized_version"
  echo "Set APP_VERSION: $current_version -> $normalized_version"
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
