#!/usr/bin/env bash
set -euo pipefail

# Windows publisher.
# Builds or reuses the signed Windows installer EXE, publishes/uploads a GitHub
# release, and writes and signs an update manifest for the selected channel.
#
# Usage:
#   publish.sh --version=<version> [--notes=<release_notes>] [--use-existing-artifacts] [--preview]
#
# Example:
#   publish.sh --version=1.0.2 --notes="Bug fixes and stability improvements"
#
use_existing_artifacts=false
preview=false

print_usage() {
  cat <<'EOF'
Usage:
  publish.sh --version=<version> [--notes=<release_notes>] [--use-existing-artifacts] [--preview]
  publish.sh --help

Options:
  --version=<version>      Release version, for example 1.0.2
  --notes=<notes>          Release notes (default: "Release <version>")
  --use-existing-artifacts  Publish existing artifacts without rebuilding
  --preview             Mark the GitHub release as a prerelease and write preview.json

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
    --preview)
      preview=true
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
  echo "Error: CAVEVIEWER_RELEASE_SIGNING_PRIVATE_KEY must be set when publishing signed Windows update manifests."
  exit 1
fi

normalized_version="${version#v}"
tag="v$normalized_version"
release_title="$tag"

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "$script_dir/../.." && pwd)"
source "$repo_root/scripts/common/version.sh"
source "$repo_root/scripts/common/github.sh"
source "$repo_root/scripts/common/release_channel.sh"

version_file="$repo_root/src/caveviewer/version.py"
windows_packages_dir="$repo_root/dist/windows/packages"
windows_metadata_dir="$repo_root/dist/windows/metadata"
manifest_channel="stable"
$preview && manifest_channel="preview"
export CAVEVIEWER_BUILD_RELEASE_CHANNEL="$manifest_channel"
cv_prepare_release_metadata "$repo_root" >/dev/null
update_manifest_path="$repo_root/updates/windows/$manifest_channel.json"
update_manifest_signature_path="$update_manifest_path.sig"

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
echo "Preview: $preview"

if [ ! -f "$version_file" ]; then
  echo "Error: version file not found: $version_file"
  exit 1
fi

if ! grep -q '^APP_VERSION = "' "$version_file"; then
  echo "Error: APP_VERSION assignment not found in $version_file"
  exit 1
fi

app_exe_name="CaveViewer-${normalized_version}-windows.exe"
app_meta_name="CaveViewer-${normalized_version}.json"
app_update_meta_name="CaveViewer-${normalized_version}.update.json"
app_exe_path="$windows_packages_dir/$app_exe_name"
app_meta_path="$windows_metadata_dir/$app_meta_name"
app_update_meta_path="$windows_metadata_dir/$app_update_meta_name"
metadata_verifier="$script_dir/verify_package_metadata.py"

current_version="$(cv_read_app_version "$version_file")"
if [ "$current_version" != "$normalized_version" ]; then
  cv_set_app_version "$version_file" "$normalized_version"
  echo "Bumped APP_VERSION: $current_version -> $normalized_version"
else
  echo "APP_VERSION already at $normalized_version"
fi

if $use_existing_artifacts; then
  echo "Using existing package artifacts (--use-existing-artifacts)."
else
  "$script_dir/package.sh" --base-download-url "https://github.com/$repo/releases/download/$tag"
fi

if [ ! -f "$app_exe_path" ]; then
  echo "Error: expected Windows installer package not found: $app_exe_path"
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

if [ ! -f "$metadata_verifier" ]; then
  echo "Error: Windows package metadata verifier is missing: $metadata_verifier"
  exit 1
fi
if [ -x "$repo_root/.venv-windows-build/Scripts/python.exe" ]; then
  metadata_python="$repo_root/.venv-windows-build/Scripts/python.exe"
elif command -v python >/dev/null 2>&1; then
  metadata_python="python"
else
  echo "Error: Python is required to verify Windows package metadata."
  exit 1
fi
"$metadata_python" "$metadata_verifier" \
  --artifact-file "$app_exe_path" \
  --metadata-file "$app_meta_path" \
  --update-metadata-file "$app_update_meta_path" \
  --release-channel "$manifest_channel"
authenticode_certificate_subject="$(
  "$metadata_python" -c \
    'import json, sys; value = json.load(open(sys.argv[1], encoding="utf-8")).get("authenticode_certificate_subject"); isinstance(value, str) and value.strip() or sys.exit("Error: Windows update metadata has no Authenticode certificate subject."); print(value.strip())' \
    "$app_update_meta_path"
)"

if gh release view "$tag" --repo "$repo" >/dev/null 2>&1; then
  echo "Release $tag already exists; uploading/replacing assets"
  gh release upload "$tag" "$app_exe_path" "$app_meta_path" "$app_update_meta_path" --repo "$repo" --clobber
else
  echo "Creating release $tag and uploading Windows assets"
  create_args=("$tag" "$app_exe_path" "$app_meta_path" "$app_update_meta_path" --repo "$repo" --title "$release_title" --notes "$release_notes")
  $preview && create_args+=(--prerelease)
  gh release create "${create_args[@]}"
fi

installer_asset_url="$(gh api "repos/$repo/releases/tags/$tag" --jq ".assets[] | select(.name == \"$app_exe_name\") | .browser_download_url")"

if [ -z "$installer_asset_url" ]; then
  echo "Error: could not resolve browser_download_url for asset $app_exe_name on release $tag"
  exit 1
fi

echo "Windows installer asset URL: $installer_asset_url"

"$script_dir/update_manifest.sh" \
  --version "$normalized_version" \
  --download-url "$installer_asset_url" \
  --artifact-file "$app_exe_path" \
  --notes "$release_notes" \
  --channel "$manifest_channel" \
  --authenticode-certificate-subject "$authenticode_certificate_subject"

signing_python="${CAVEVIEWER_RELEASE_SIGNING_PYTHON:-}"
if [ -z "$signing_python" ]; then
  if [ -x "$repo_root/.venv-dev/Scripts/python.exe" ]; then
    signing_python="$repo_root/.venv-dev/Scripts/python.exe"
  elif [ -x "$repo_root/.venv-dev/bin/python" ]; then
    signing_python="$repo_root/.venv-dev/bin/python"
  elif command -v python3 >/dev/null 2>&1; then
    signing_python="python3"
  else
    signing_python="python"
  fi
fi

# A newer manifest is rejected by every installed client unless its exact
# bytes have a companion Ed25519 signature made by the release key.
echo "Signing Windows $manifest_channel update manifest: $update_manifest_path"
"$signing_python" "$repo_root/scripts/sign_update_manifest.py" \
  "$update_manifest_path" \
  --signature "$update_manifest_signature_path"

echo "Committing version bump and updated signed $manifest_channel manifest..."
git -C "$repo_root" add \
  src/caveviewer/version.py \
  packaging/linux/io.github.caveviewer.caveviewer.metainfo.xml \
  "updates/windows/$manifest_channel.json" \
  "updates/windows/$manifest_channel.json.sig"
git -C "$repo_root" commit -m "Release $tag Windows $manifest_channel"
git -C "$repo_root" push

echo "Done. Release $tag is published and the signed $manifest_channel manifest is live."
