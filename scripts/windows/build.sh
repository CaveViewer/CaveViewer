#!/usr/bin/env bash
set -euo pipefail

# Build the frozen Windows x64 one-folder payload consumed by CaveViewerSetup.
# This script must run from a native Windows shell (Git Bash is supported).

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "$script_dir/../.." && pwd)"
source "$repo_root/scripts/common/python.sh"
source "$repo_root/scripts/common/release_channel.sh"

print_usage() {
  cat <<'EOF'
Usage:
  build.sh
  build.sh --help

Build the Windows x64 frozen one-folder CaveViewer payload under
dist/windows/app/CaveViewer. Run this from native Windows; Git Bash is
supported. Set CAVEVIEWER_WINDOWS_BUILD_PYTHON to a prepared Python 3.12
interpreter to use it instead of .venv-windows-build.
EOF
}

is_windows_host() {
  case "${OS:-}:$(uname -s)" in
    Windows_NT:*|*:MINGW*|*:MSYS*|*:CYGWIN*) return 0 ;;
    *) return 1 ;;
  esac
}

windows_path() {
  local path="$1"
  if command -v cygpath >/dev/null 2>&1; then
    cygpath -aw "$path"
  else
    printf '%s\n' "$path"
  fi
}

create_build_venv() {
  local venv_dir="$1"
  if command -v py.exe >/dev/null 2>&1; then
    py.exe -3.12 -m venv "$venv_dir"
  elif command -v py >/dev/null 2>&1; then
    py -3.12 -m venv "$venv_dir"
  elif command -v python.exe >/dev/null 2>&1; then
    python.exe -m venv "$venv_dir"
  elif command -v python >/dev/null 2>&1; then
    python -m venv "$venv_dir"
  else
    echo "Error: Python 3.12 is required to create the Windows build environment." >&2
    return 1
  fi
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    -h|--help)
      print_usage
      exit 0
      ;;
    -*)
      echo "Error: unknown option '$1'" >&2
      print_usage >&2
      exit 1
      ;;
    *)
      echo "Error: positional arguments are not supported: '$1'" >&2
      print_usage >&2
      exit 1
      ;;
  esac
done

if ! is_windows_host; then
  echo "Error: scripts/windows/build.sh requires a native Windows host." >&2
  exit 1
fi

version_file="$repo_root/src/caveviewer/version.py"
spec_file="$repo_root/packaging/pyinstaller/CaveViewer.spec"
venv_dir="${CAVEVIEWER_WINDOWS_BUILD_VENV:-$repo_root/.venv-windows-build}"
dist_app_dir="$repo_root/dist/windows/app"
payload_dir="$dist_app_dir/CaveViewer"
work_dir="$repo_root/build/pyinstaller/windows"
branding_export_dir="$repo_root/build/branding/windows"
branding_profile="${CAVEVIEWER_BRAND_PROFILE:-$repo_root/src/caveviewer/resources/branding/default}"
build_python="${CAVEVIEWER_WINDOWS_BUILD_PYTHON:-}"

cv_prepare_release_metadata "$repo_root" >/dev/null
release_metadata_path="$CAVEVIEWER_RELEASE_METADATA_PATH"

for required_path in "$version_file" "$spec_file"; do
  if [ ! -f "$required_path" ]; then
    echo "Error: required Windows build input is missing: $required_path" >&2
    exit 1
  fi
done

if [ -z "$build_python" ]; then
  build_python="$venv_dir/Scripts/python.exe"
  if [ ! -x "$build_python" ]; then
    mkdir -p "$(dirname "$venv_dir")"
    create_build_venv "$venv_dir"
  fi
fi

if ! cv_python_is_supported "$build_python"; then
  echo "Error: Windows payload builds require Python $CV_PYTHON_SERIES: $build_python" >&2
  exit 1
fi

"$build_python" -m pip install --upgrade pip
"$build_python" -m pip install -r "$repo_root/requirements.txt"
"$build_python" -m pip install --no-deps -e "$(windows_path "$repo_root")"
"$build_python" -m pip install "pyinstaller==6.21.0"

"$build_python" -m caveviewer.branding_export \
  --profile "$(windows_path "$branding_profile")" \
  export \
  --output "$(windows_path "$branding_export_dir")" \
  --replace
icon_file="$branding_export_dir/windows/caveviewer.ico"
branding_summary="$branding_export_dir/export-summary.v1.json"
if [ -d "$branding_profile" ]; then
  branding_profile_dir="$branding_profile"
else
  branding_profile_dir="$(dirname "$branding_profile")"
fi
for required_path in "$icon_file" "$branding_summary" "$branding_profile_dir/branding.v2.json"; do
  if [ ! -f "$required_path" ]; then
    echo "Error: required branding export input is missing: $required_path" >&2
    exit 1
  fi
done

rm -rf "$payload_dir" "$work_dir"
mkdir -p "$dist_app_dir" "$work_dir"

CAVEVIEWER_APP_ICON="$(windows_path "$icon_file")" \
CAVEVIEWER_BRAND_PROFILE_DIR="$(windows_path "$branding_profile_dir")" \
CAVEVIEWER_BRANDING_EXPORT_SUMMARY="$(windows_path "$branding_summary")" \
CAVEVIEWER_RELEASE_METADATA_PATH="$(windows_path "$release_metadata_path")" \
"$build_python" -m PyInstaller --clean --noconfirm \
  --distpath "$(windows_path "$dist_app_dir")" \
  --workpath "$(windows_path "$work_dir")" \
  "$(windows_path "$spec_file")"

if [ ! -f "$payload_dir/CaveViewer.exe" ]; then
  echo "Error: PyInstaller did not create CaveViewer.exe: $payload_dir" >&2
  exit 1
fi
for required_name in LICENSE THIRD_PARTY_NOTICES.md; do
  if ! find "$payload_dir" -type f -name "$required_name" -print -quit | grep -q .; then
    echo "Error: frozen payload is missing $required_name." >&2
    exit 1
  fi
done
if ! find "$payload_dir" -type d -path '*caveviewer/resources/shaders' -print -quit | grep -q .; then
  echo "Error: frozen payload is missing CaveViewer shader resources." >&2
  exit 1
fi
for signing_identity in primary recovery legacy; do
  signing_key_name="release_signing_${signing_identity}_public_key.pem"
  if ! find "$payload_dir" -type f -name "$signing_key_name" -print -quit | grep -q .; then
    echo "Error: frozen payload is missing the $signing_identity update-signing public key." >&2
    exit 1
  fi
done
bundled_release_metadata="$(find "$payload_dir" -type f -path '*caveviewer/resources/release_metadata.v1.json' -print -quit)"
if [ -z "$bundled_release_metadata" ]; then
  echo "Error: frozen payload is missing embedded release metadata." >&2
  exit 1
fi
cv_verify_release_metadata "$bundled_release_metadata" "$(cv_release_channel)"
bundled_branding_manifest="$(find "$payload_dir" -type f -path '*caveviewer/resources/branding/default/branding.v2.json' -print -quit)"
bundled_branding_summary="$(find "$payload_dir" -type f -path '*caveviewer/resources/branding/export-summary.v1.json' -print -quit)"
if [ -z "$bundled_branding_manifest" ] || [ -z "$bundled_branding_summary" ]; then
  echo "Error: frozen payload is missing selected branding inputs or provenance." >&2
  exit 1
fi

echo "Built frozen Windows payload: $payload_dir"
echo "Branding export summary: $branding_summary"
