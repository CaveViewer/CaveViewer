#!/usr/bin/env bash
set -euo pipefail

# macOS app bundle builder.
# Builds the intermediate CaveViewer.app bundle with PyInstaller under
# dist/macos/app for later DMG packaging.

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "$script_dir/../.." && pwd)"

print_usage() {
  cat <<'EOF'
Usage:
  build.sh [--help]

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
spec_file="$repo_root/CaveViewer.spec"
dist_app_dir="$repo_root/dist/macos/app"
work_dir="$repo_root/build/pyinstaller"
logo_png="$repo_root/gui/assets/app_icon_macos.png"
icon_work_dir="$work_dir/iconset"
iconset_dir="$icon_work_dir/CaveViewer.iconset"
icon_icns="$icon_work_dir/CaveViewer.icns"

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "Error: this script must be run on macOS."
  exit 1
fi

if [ ! -f "$spec_file" ]; then
  echo "Error: missing spec file: $spec_file"
  exit 1
fi

if ! command -v python3 >/dev/null 2>&1; then
  echo "Error: python3 not found. Install Python 3.10+ and re-run."
  exit 1
fi

if [ ! -x "$venv_dir/bin/python" ] || ! "$venv_dir/bin/python" -c "import sys" >/dev/null 2>&1; then
  if [ -d "$venv_dir" ]; then
    echo "Existing macOS build virtual environment at $venv_dir is invalid; recreating it."
    rm -rf "$venv_dir"
  fi
  echo "Creating macOS build virtual environment at $venv_dir"
  python3 -m venv "$venv_dir"
fi

if [ ! -f "$logo_png" ]; then
  echo "Error: app logo source not found at $logo_png"
  exit 1
fi

if ! command -v sips >/dev/null 2>&1; then
  echo "Error: required macOS tool not found: sips"
  exit 1
fi

if ! command -v iconutil >/dev/null 2>&1; then
  echo "Error: required macOS tool not found: iconutil"
  exit 1
fi

echo "Using venv: $venv_dir"
"$venv_dir/bin/python" -m pip install --upgrade -r "$repo_root/requirements.txt"
"$venv_dir/bin/python" -m pip install --upgrade "pyinstaller==6.21.0"

cd "$repo_root"
mkdir -p "$dist_app_dir" "$work_dir"
rm -rf "$iconset_dir"
mkdir -p "$iconset_dir"

make_icon_png() {
  local size="$1"
  local out_name="$2"
  sips -z "$size" "$size" "$logo_png" --out "$iconset_dir/$out_name" >/dev/null
}

make_icon_png 16 icon_16x16.png
make_icon_png 32 icon_16x16@2x.png
make_icon_png 32 icon_32x32.png
make_icon_png 64 icon_32x32@2x.png
make_icon_png 128 icon_128x128.png
make_icon_png 256 icon_128x128@2x.png
make_icon_png 256 icon_256x256.png
make_icon_png 512 icon_256x256@2x.png
make_icon_png 512 icon_512x512.png
make_icon_png 1024 icon_512x512@2x.png

rm -f "$icon_icns"
iconutil -c icns "$iconset_dir" -o "$icon_icns"

CAVEVIEWER_APP_ICON="$icon_icns" \
"$venv_dir/bin/python" -m PyInstaller --clean --noconfirm \
  --distpath "$dist_app_dir" \
  --workpath "$work_dir" \
  "$spec_file"

app_path="$dist_app_dir/CaveViewer.app"
if [ ! -d "$app_path" ]; then
  echo "Error: build completed but app not found at $app_path"
  exit 1
fi

echo "Build complete: $app_path"
echo "App icon source: $logo_png"
echo "Generated app icon: $icon_icns"
echo "Note: CaveViewer.app is an intermediate build artifact."
echo "Run ./scripts/macos/package.sh to generate the distributable DMG in dist/macos/packages/."
