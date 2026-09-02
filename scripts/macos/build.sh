#!/usr/bin/env bash
set -euo pipefail

# macOS app bundle builder.
# Builds the intermediate CaveViewer.app bundle with PyInstaller under
# dist/macos/app for later DMG packaging.

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "$script_dir/../.." && pwd)"
source "$script_dir/../common/python.sh"
source "$script_dir/../common/release_channel.sh"

print_usage() {
  cat <<'EOF'
Usage:
  build.sh --help

Builds the intermediate macOS CaveViewer.app bundle.
EOF
}

if [ "${1:-}" = "-h" ] || [ "${1:-}" = "--help" ]; then
  print_usage
  exit 0
fi

if [ "$#" -gt 0 ]; then
  if [[ "$1" == -* ]]; then
    echo "Error: unknown option '$1'"
  else
    echo "Error: positional arguments are not supported: '$1'"
  fi
  echo ""
  print_usage
  exit 1
fi

venv_dir="${CAVEVIEWER_MACOS_BUILD_VENV:-$repo_root/.venv-macos-build}"
venv_python="$venv_dir/bin/python"
spec_file="$repo_root/packaging/pyinstaller/CaveViewer.spec"
dist_app_dir="$repo_root/dist/macos/app"
work_dir="$repo_root/build/pyinstaller"
branding_export_dir="$repo_root/build/branding/macos"
branding_profile="${CAVEVIEWER_BRAND_PROFILE:-$repo_root/src/caveviewer/resources/branding/default}"
iconset_dir="$branding_export_dir/macos/CaveViewer.iconset"
icon_icns="$branding_export_dir/macos/CaveViewer.icns"

cv_prepare_release_metadata "$repo_root" >/dev/null
release_metadata_path="$CAVEVIEWER_RELEASE_METADATA_PATH"

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "Error: this script must be run on macOS."
  exit 1
fi

if [ ! -f "$spec_file" ]; then
  echo "Error: missing spec file: $spec_file"
  exit 1
fi

python_bin="$(cv_resolve_project_python)"

if [ ! -x "$venv_python" ] || ! cv_python_is_supported "$venv_python"; then
  if [ -d "$venv_dir" ]; then
    echo "Existing macOS build virtual environment at $venv_dir is invalid; recreating it."
    rm -rf "$venv_dir"
  fi
  echo "Creating macOS build virtual environment at $venv_dir"
  "$python_bin" -m venv "$venv_dir"
fi

if ! command -v iconutil >/dev/null 2>&1; then
  echo "Error: required macOS tool not found: iconutil"
  exit 1
fi

echo "Using venv: $venv_dir"
"$venv_python" -m pip install --upgrade -r "$repo_root/requirements.txt"
"$venv_python" -m pip install --no-deps -e "$repo_root"
"$venv_python" -m pip install --upgrade "pyinstaller==6.21.0"

cd "$repo_root"
mkdir -p "$dist_app_dir" "$work_dir"
"$venv_python" -m caveviewer.branding_export \
  --profile "$branding_profile" \
  export \
  --output "$branding_export_dir" \
  --replace
branding_summary="$branding_export_dir/export-summary.v1.json"
if [ -d "$branding_profile" ]; then
  branding_profile_dir="$branding_profile"
else
  branding_profile_dir="$(dirname "$branding_profile")"
fi
for required_path in "$iconset_dir/icon_512x512@2x.png" "$branding_summary" "$branding_profile_dir/branding.v2.json"; do
  if [ ! -f "$required_path" ]; then
    echo "Error: required macOS branding output is missing: $required_path" >&2
    exit 1
  fi
done

rm -f "$icon_icns"
iconutil -c icns "$iconset_dir" -o "$icon_icns"

CAVEVIEWER_APP_ICON="$icon_icns" \
CAVEVIEWER_BRAND_PROFILE_DIR="$branding_profile_dir" \
CAVEVIEWER_BRANDING_EXPORT_SUMMARY="$branding_summary" \
CAVEVIEWER_RELEASE_METADATA_PATH="$release_metadata_path" \
"$venv_dir/bin/python" -m PyInstaller --clean --noconfirm \
  --distpath "$dist_app_dir" \
  --workpath "$work_dir" \
  "$spec_file"

app_path="$dist_app_dir/CaveViewer.app"
if [ ! -d "$app_path" ]; then
  echo "Error: build completed but app not found at $app_path"
  exit 1
fi
bundled_release_metadata="$(find "$app_path" -type f -path '*caveviewer/resources/release_metadata.v1.json' -print -quit)"
if [ -z "$bundled_release_metadata" ]; then
  echo "Error: macOS app bundle is missing embedded release metadata." >&2
  exit 1
fi
cv_verify_release_metadata "$bundled_release_metadata" "$(cv_release_channel)"
bundled_branding_manifest="$(find "$app_path" -type f -path '*caveviewer/resources/branding/default/branding.v2.json' -print -quit)"
bundled_branding_summary="$(find "$app_path" -type f -path '*caveviewer/resources/branding/export-summary.v1.json' -print -quit)"
if [ -z "$bundled_branding_manifest" ] || [ -z "$bundled_branding_summary" ]; then
  echo "Error: macOS app bundle is missing selected branding inputs or provenance." >&2
  exit 1
fi

echo "Build complete: $app_path"
echo "Branding export summary: $branding_summary"
echo "Generated app icon: $icon_icns"
echo "Note: CaveViewer.app is an intermediate build artifact."
echo "Run ./scripts/macos/package.sh to generate the distributable DMG in dist/macos/packages/."
