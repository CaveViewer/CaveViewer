#!/usr/bin/env bash
set -euo pipefail

# Build package artifacts for all supported platforms from one entrypoint.
#
# Usage:
#   ./scripts/all_package.sh [--linux-arch=arm64|amd64|both] [--rebuild] [--skip=macos,linux,windows]
#
# Notes:
# - linux arch defaults to "both".
# - Existing per-platform scripts remain the source of truth.

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "$script_dir/.." && pwd)"

source "$script_dir/common/version.sh"
source "$script_dir/common/artifacts.sh"

linux_arch="both"
rebuild=false
skip_macos=false
skip_linux=false
skip_windows=false

print_help() {
  cat <<'EOF'
Usage:
  ./scripts/all_package.sh [options]

Options:
  --linux-arch=arm64|amd64|both   Linux build architecture(s). Default: both
  --rebuild                        Force Docker Linux image rebuild
  --skip=macos,linux,windows       Comma-separated targets to skip
  -h, --help                       Show this help
EOF
}

for arg in "$@"; do
  case "$arg" in
    --linux-arch=*)
      linux_arch="${arg#--linux-arch=}"
      ;;
    --rebuild)
      rebuild=true
      ;;
    --skip=*)
      skip_list="${arg#--skip=}"
      IFS=',' read -r -a items <<<"$skip_list"
      for item in "${items[@]}"; do
        case "${item// /}" in
          macos) skip_macos=true ;;
          linux) skip_linux=true ;;
          windows) skip_windows=true ;;
          "") ;;
          *)
            echo "Error: unknown skip target '$item'"
            exit 1
            ;;
        esac
      done
      ;;
    -h|--help)
      print_help
      exit 0
      ;;
    *)
      echo "Error: unknown option '$arg'"
      print_help
      exit 1
      ;;
  esac
done

case "$linux_arch" in
  arm64|amd64|both) ;;
  *)
    echo "Error: invalid --linux-arch '$linux_arch' (expected arm64|amd64|both)"
    exit 1
    ;;
esac

host_os="$(uname -s)"

echo "====================================================="
echo "CaveViewer unified packaging"
echo "Host OS: $host_os"
echo "Linux arch: $linux_arch"
echo "====================================================="

run_linux_native=false
run_linux_docker=false
if [ "$host_os" = "Darwin" ]; then
  run_linux_docker=true
elif [ "$host_os" = "Linux" ]; then
  native_arch=""
  case "$(uname -m)" in
    x86_64) native_arch="amd64" ;;
    aarch64) native_arch="arm64" ;;
  esac

  if [ "$linux_arch" = "both" ] || [ "$linux_arch" != "$native_arch" ]; then
    if command -v docker >/dev/null 2>&1; then
      run_linux_docker=true
    else
      if [ "$linux_arch" = "both" ]; then
        echo "[linux] Docker not found; falling back to native-only build."
        run_linux_native=true
      else
        echo "Error: requested linux arch '$linux_arch' on native '$native_arch' host without Docker."
        exit 1
      fi
    fi
  else
    run_linux_native=true
  fi
else
  echo "[linux] Unsupported host for Linux packaging: $host_os"
fi

if ! $skip_macos; then
  if [ "$host_os" = "Darwin" ]; then
    echo "[macos] Building package..."
    "$script_dir/macos/package.sh"
  else
    echo "[macos] Skipped: requires macOS host."
  fi
else
  echo "[macos] Skipped by option."
fi

if ! $skip_linux; then
  if $run_linux_docker; then
    echo "[linux] Building package(s) via Docker..."
    docker_args=("--arch=$linux_arch")
    if $rebuild; then
      docker_args+=("--rebuild")
    fi
    "$script_dir/linux/build_linux_in_docker.sh" "${docker_args[@]}"
  elif $run_linux_native; then
    echo "[linux] Building package natively..."
    "$script_dir/linux/build_linux_app.sh"
    "$script_dir/linux/package.sh"
  else
    echo "[linux] Skipped: unsupported host setup."
  fi
else
  echo "[linux] Skipped by option."
fi

if ! $skip_windows; then
  echo "[windows] Building package..."
  "$script_dir/windows/package.sh"
else
  echo "[windows] Skipped by option."
fi

version_file="$repo_root/caveviewer_version.py"
version="$(cv_read_app_version "$version_file")"

echo ""
echo "====================================================="
echo "Artifact summary (version $version)"
echo "====================================================="

print_artifact() {
  local label="$1"
  local path="$2"
  if [ -f "$path" ]; then
    local bytes sha
    bytes="$(cv_size_bytes "$path")"
    sha="$(cv_sha256 "$path")"
    echo "$label"
    echo "  path: $path"
    echo "  size_bytes: $bytes"
    echo "  sha256: $sha"
  else
    echo "$label"
    echo "  missing"
  fi
}

if [ "$host_os" = "Darwin" ] && ! $skip_macos; then
  print_artifact "macOS DMG" "$repo_root/dist/macos/packages/CaveViewer-${version}.dmg"
fi

if ! $skip_linux; then
  linux_found=false
  while IFS= read -r -d '' appimage; do
    linux_found=true
    print_artifact "Linux AppImage" "$appimage"
  done < <(find "$repo_root/dist/linux/packages" -maxdepth 1 -name "CaveViewer-${version}-*.AppImage" -print0 2>/dev/null | sort -z)
  if ! $linux_found; then
    echo "Linux AppImage"
    echo "  missing"
  fi
fi

if ! $skip_windows; then
  print_artifact "Windows ZIP" "$repo_root/dist/windows/packages/CaveViewer-${version}-windows.zip"
fi

echo ""
echo "Done."
