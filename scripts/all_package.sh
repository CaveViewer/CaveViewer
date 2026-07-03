#!/usr/bin/env bash
set -euo pipefail

# Build package artifacts for all supported platforms from one entrypoint.
#
# Usage:
#   ./scripts/all_package.sh --version=X.Y.Z [--linux-arch=arm64|amd64|both] [--rebuild] [--skip=macos,linux,windows] [--publish] [--release-notes="..."]
#
# Notes:
# - linux arch defaults to "both".
# - Existing per-platform scripts remain the source of truth.

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "$script_dir/.." && pwd)"

source "$script_dir/common/version.sh"
source "$script_dir/common/artifacts.sh"

version_file="$repo_root/caveviewer_version.py"
release_version=""
linux_arch="both"
rebuild=false
publish=false
release_notes=""
skip_macos=false
skip_linux=false
skip_windows=false

linux_artifact_exists_for_arch() {
  local arch="$1"
  local suffix=""
  case "$arch" in
    arm64) suffix="aarch64" ;;
    amd64) suffix="x86_64" ;;
    *) return 1 ;;
  esac
  [ -f "$repo_root/dist/linux/packages/CaveViewer-${normalized_version}-${suffix}.AppImage" ]
}

linux_artifacts_ready() {
  if $run_linux_docker; then
    case "$linux_arch" in
      both)
        linux_artifact_exists_for_arch arm64 && linux_artifact_exists_for_arch amd64
        ;;
      arm64|amd64)
        linux_artifact_exists_for_arch "$linux_arch"
        ;;
      *)
        return 1
        ;;
    esac
    return
  fi

  if $run_linux_native; then
    local target_arch="$linux_arch"
    if [ "$target_arch" = "both" ]; then
      target_arch="$native_arch"
    fi
    [ -n "$target_arch" ] && linux_artifact_exists_for_arch "$target_arch"
    return
  fi

  return 1
}

print_help() {
  cat <<'EOF'
Usage:
  ./scripts/all_package.sh --version=X.Y.Z [options]

Options:
  --version=X.Y.Z                   Required. Set APP_VERSION before packaging (accepts optional leading v)
  --linux-arch=arm64|amd64|both    Linux build architecture(s). Default: both
  --rebuild                         Force Docker Linux image rebuild
  --publish                         After build succeeds, publish artifacts to GitHub via platform publish scripts
  --release-notes="text"            Release notes used when --publish is set (default: "Release X.Y.Z")
  --skip=macos,linux,windows        Comma-separated targets to skip
  -h, --help                        Show this help
EOF
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --version=*)
      release_version="${1#--version=}"
      shift
      ;;
    --version)
      shift
      if [ "$#" -eq 0 ]; then
        echo "Error: --version requires a value"
        exit 1
      fi
      release_version="$1"
      shift
      ;;
    --linux-arch=*)
      linux_arch="${1#--linux-arch=}"
      shift
      ;;
    --rebuild)
      rebuild=true
      shift
      ;;
    --publish)
      publish=true
      shift
      ;;
    --release-notes=*)
      release_notes="${1#--release-notes=}"
      shift
      ;;
    --release-notes)
      shift
      if [ "$#" -eq 0 ]; then
        echo "Error: --release-notes requires a value"
        exit 1
      fi
      release_notes="$1"
      shift
      ;;
    --skip=*)
      skip_list="${1#--skip=}"
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
      shift
      ;;
    -h|--help)
      print_help
      exit 0
      ;;
    *)
      echo "Error: unknown option '$1'"
      print_help
      exit 1
      ;;
  esac
done

if [ -z "$release_version" ]; then
  echo "Error: --version is required"
  print_help
  exit 1
fi

case "$linux_arch" in
  arm64|amd64|both) ;;
  *)
    echo "Error: invalid --linux-arch '$linux_arch' (expected arm64|amd64|both)"
    exit 1
    ;;
esac

normalized_version="${release_version#v}"
if [ -z "$normalized_version" ]; then
  echo "Error: --version cannot be empty"
  exit 1
fi

current_version="$(cv_read_app_version "$version_file")"
if [ -z "$current_version" ]; then
  echo "Error: could not read APP_VERSION from $version_file"
  exit 1
fi

if [ "$current_version" != "$normalized_version" ]; then
  cv_set_app_version "$version_file" "$normalized_version"
  echo "Set APP_VERSION: $current_version -> $normalized_version"
else
  echo "APP_VERSION already at $normalized_version"
fi

host_os="$(uname -s)"
effective_release_notes="$release_notes"
if [ -z "$effective_release_notes" ]; then
  effective_release_notes="Release $normalized_version"
fi

echo "====================================================="
echo "CaveViewer unified packaging"
echo "Host OS: $host_os"
echo "Linux arch: $linux_arch"
echo "Publish mode: $publish"
echo "====================================================="

run_linux_native=false
run_linux_docker=false
native_arch=""
if [ "$host_os" = "Darwin" ]; then
  run_linux_docker=true
elif [ "$host_os" = "Linux" ]; then
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
    macos_dmg_path="$repo_root/dist/macos/packages/CaveViewer-${normalized_version}.dmg"
    if $publish && ! $rebuild && [ -f "$macos_dmg_path" ]; then
      echo "[macos] Reusing existing package: $macos_dmg_path"
    else
      echo "[macos] Building package..."
      "$script_dir/macos/package.sh"
    fi
  else
    echo "[macos] Skipped: requires macOS host."
  fi
else
  echo "[macos] Skipped by option."
fi

if ! $skip_linux; then
  if $publish && ! $rebuild && linux_artifacts_ready; then
    echo "[linux] Reusing existing package artifact(s) for version $normalized_version."
  else
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
  fi
else
  echo "[linux] Skipped by option."
fi

if ! $skip_windows; then
  windows_zip_path="$repo_root/dist/windows/packages/CaveViewer-${normalized_version}-windows.zip"
  if $publish && ! $rebuild && [ -f "$windows_zip_path" ]; then
    echo "[windows] Reusing existing package: $windows_zip_path"
  else
    echo "[windows] Building package..."
    "$script_dir/windows/package.sh"
  fi
else
  echo "[windows] Skipped by option."
fi

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

if $publish; then
  echo ""
  echo "====================================================="
  echo "Publishing artifacts"
  echo "====================================================="

  if ! $skip_macos; then
    if [ "$host_os" = "Darwin" ]; then
      echo "[macos] Publishing release assets..."
        "$script_dir/macos/publish_release.sh" --skip-build "$normalized_version" "$effective_release_notes"
    else
      echo "[macos] Skipped publish: requires macOS host."
    fi
  else
    echo "[macos] Publish skipped by option."
  fi

  if ! $skip_linux; then
    if $run_linux_docker || $run_linux_native; then
      echo "[linux] Publishing release assets..."
        "$script_dir/linux/publish_release.sh" --skip-build "$normalized_version" "$effective_release_notes"
    else
      echo "[linux] Skipped publish: unsupported host setup."
    fi
  else
    echo "[linux] Publish skipped by option."
  fi

  if ! $skip_windows; then
    echo "[windows] Publishing release assets..."
      "$script_dir/windows/publish_release.sh" --skip-build "$normalized_version" "$effective_release_notes"
  else
    echo "[windows] Publish skipped by option."
  fi
fi

echo ""
echo "Done."
