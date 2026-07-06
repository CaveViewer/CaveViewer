#!/usr/bin/env bash
set -euo pipefail

# Build package artifacts for explicitly selected release targets.
#
# Usage:
#   ./scripts/all_package.sh --version=X.Y.Z [--targets=macos,linux-arm64,linux-x86_64,windows] [--rebuild] [--publish] [--pre-release] [--release-notes="..."]
#
# Notes:
# - targets default to "all".
# - Existing per-target scripts remain the source of truth.

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "$script_dir/.." && pwd)"

source "$script_dir/common/version.sh"
source "$script_dir/common/artifacts.sh"

version_file="$repo_root/caveviewer_version.py"
release_version=""
targets_arg="all"
rebuild=false
publish=false
pre_release=false
release_notes=""
target_macos=false
target_windows=false
target_linux_arm64=false
target_linux_x86_64=false

print_help() {
  cat <<'EOF'
Usage:
  ./scripts/all_package.sh --version=X.Y.Z [options]

Options:
  --version=X.Y.Z                   Required. Set APP_VERSION before packaging (accepts optional leading v)
  --targets=target1,target2         Build targets. Default: all
                                    Targets: macos, windows, linux-arm64, linux-x86_64
                                    Groups: all, linux
  --rebuild                         Force Linux Docker image rebuild
  --publish                         After build succeeds, publish artifacts to GitHub via target publish scripts
  --pre-release                     Mark created GitHub releases as prereleases; only used with --publish
  --release-notes="text"            Release notes used when --publish is set (default: "Release X.Y.Z")
  -h, --help                        Show this help

Removed options:
  --linux-arch has been replaced by --targets=linux-arm64 or --targets=linux-x86_64
  --skip has been replaced by explicitly listing desired --targets
EOF
}

add_target() {
  local target="$1"
  case "$target" in
    all)
      target_macos=true
      target_windows=true
      target_linux_arm64=true
      target_linux_x86_64=true
      ;;
    linux)
      target_linux_arm64=true
      target_linux_x86_64=true
      ;;
    macos)
      target_macos=true
      ;;
    windows)
      target_windows=true
      ;;
    linux-arm64|linux-aarch64)
      target_linux_arm64=true
      ;;
    linux-x86_64|linux-amd64)
      target_linux_x86_64=true
      ;;
    "")
      ;;
    *)
      echo "Error: unknown target '$target'"
      echo "Expected one of: all, macos, windows, linux, linux-arm64, linux-x86_64"
      exit 1
      ;;
  esac
}

parse_targets() {
  old_ifs="$IFS"
  IFS=','
  for item in $targets_arg; do
    IFS="$old_ifs"
    add_target "${item// /}"
    IFS=','
  done
  IFS="$old_ifs"
}

has_linux_target() {
  $target_linux_arm64 || $target_linux_x86_64
}

selected_linux_arch() {
  if $target_linux_arm64 && $target_linux_x86_64; then
    echo "both"
  elif $target_linux_arm64; then
    echo "arm64"
  elif $target_linux_x86_64; then
    echo "x86_64"
  else
    echo "none"
  fi
}

target_summary() {
  local targets=()
  $target_macos && targets+=("macos")
  $target_linux_arm64 && targets+=("linux-arm64")
  $target_linux_x86_64 && targets+=("linux-x86_64")
  $target_windows && targets+=("windows")
  if [ "${#targets[@]}" -eq 0 ]; then
    echo "none"
  else
    local old_ifs="$IFS"
    IFS=','
    echo "${targets[*]}"
    IFS="$old_ifs"
  fi
}

linux_artifact_exists_for_arch() {
  local arch="$1"
  local suffix="" arch_dir=""
  case "$arch" in
    arm64)
      suffix="aarch64"
      arch_dir="arm64"
      ;;
    x86_64|amd64)
      suffix="x86_64"
      arch_dir="x86_64"
      ;;
    *) return 1 ;;
  esac
  [ -f "$repo_root/dist/linux/$arch_dir/packages/CaveViewer-${normalized_version}-${suffix}.AppImage" ]
}

linux_artifacts_ready() {
  local linux_arch="$1"
  case "$linux_arch" in
    both)
      linux_artifact_exists_for_arch arm64 && linux_artifact_exists_for_arch x86_64
      ;;
    arm64|x86_64)
      linux_artifact_exists_for_arch "$linux_arch"
      ;;
    *)
      return 1
      ;;
  esac
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
    --targets=*)
      targets_arg="${1#--targets=}"
      shift
      ;;
    --targets)
      shift
      if [ "$#" -eq 0 ]; then
        echo "Error: --targets requires a comma-separated value"
        exit 1
      fi
      targets_arg="$1"
      shift
      ;;
    --linux-arch|--linux-arch=*)
      echo "Error: --linux-arch has been removed. Use --targets=linux-arm64, --targets=linux-x86_64, or --targets=linux."
      exit 1
      ;;
    --skip|--skip=*)
      echo "Error: --skip has been removed. Use --targets to list the release targets you want."
      echo "Example: --targets=macos,linux-arm64"
      exit 1
      ;;
    --linux-build|--linux-build=*)
      echo "Error: --linux-build has been removed. Linux release builds always use Docker."
      exit 1
      ;;
    --rebuild)
      rebuild=true
      shift
      ;;
    --publish)
      publish=true
      shift
      ;;
    --pre-release)
      pre_release=true
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

if $pre_release && ! $publish; then
  echo "Error: --pre-release is only valid with --publish"
  exit 1
fi

parse_targets
linux_arch="$(selected_linux_arch)"

if [ "$(target_summary)" = "none" ]; then
  echo "Error: no build targets selected"
  exit 1
fi

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
echo "Targets: $(target_summary)"
echo "Linux build mode: docker"
echo "Publish mode: $publish"
echo "Prerelease mode: $pre_release"
echo "====================================================="

if has_linux_target; then
  if ! command -v docker >/dev/null 2>&1; then
    echo "Error: Docker is required for Linux release builds."
    exit 1
  fi
fi

if $target_macos; then
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
fi

if has_linux_target; then
  if $publish && ! $rebuild && linux_artifacts_ready "$linux_arch"; then
    echo "[linux] Reusing existing package artifact(s) for version $normalized_version."
  else
    echo "[linux] Building package(s) via Docker..."
    if $rebuild; then
      "$script_dir/linux/build_linux_in_docker.sh" "--arch=$linux_arch" --rebuild
    else
      "$script_dir/linux/build_linux_in_docker.sh" "--arch=$linux_arch"
    fi
  fi
fi

if $target_windows; then
  windows_zip_path="$repo_root/dist/windows/packages/CaveViewer-${normalized_version}-windows.zip"
  if $publish && ! $rebuild && [ -f "$windows_zip_path" ]; then
    echo "[windows] Reusing existing package: $windows_zip_path"
  else
    echo "[windows] Building package..."
    "$script_dir/windows/package.sh"
  fi
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

if $target_macos && [ "$host_os" = "Darwin" ]; then
  print_artifact "macOS DMG" "$repo_root/dist/macos/packages/CaveViewer-${version}.dmg"
fi

if $target_linux_arm64; then
  print_artifact "Linux ARM64 AppImage" "$repo_root/dist/linux/arm64/packages/CaveViewer-${version}-aarch64.AppImage"
fi
if $target_linux_x86_64; then
  print_artifact "Linux x86_64 AppImage" "$repo_root/dist/linux/x86_64/packages/CaveViewer-${version}-x86_64.AppImage"
fi

if $target_windows; then
  print_artifact "Windows ZIP" "$repo_root/dist/windows/packages/CaveViewer-${version}-windows.zip"
fi

if $publish; then
  echo ""
  echo "====================================================="
  echo "Publishing artifacts"
  echo "====================================================="

  publish_args=(--skip-build)
  $pre_release && publish_args+=(--pre-release)

  if $target_macos; then
    if [ "$host_os" = "Darwin" ]; then
      echo "[macos] Publishing release assets..."
      "$script_dir/macos/publish.sh" "${publish_args[@]}" "$normalized_version" "$effective_release_notes"
    else
      echo "[macos] Skipped publish: requires macOS host."
    fi
  fi

  if $target_linux_arm64; then
    if linux_artifact_exists_for_arch arm64; then
      echo "[linux-arm64] Publishing release assets..."
      "$script_dir/linux/arm64/publish.sh" "${publish_args[@]}" "$normalized_version" "$effective_release_notes"
    else
      echo "[linux-arm64] Publish skipped: artifact missing."
    fi
  fi

  if $target_linux_x86_64; then
    if linux_artifact_exists_for_arch x86_64; then
      echo "[linux-x86_64] Publishing release assets..."
      "$script_dir/linux/x86_64/publish.sh" "${publish_args[@]}" "$normalized_version" "$effective_release_notes"
    else
      echo "[linux-x86_64] Publish skipped: artifact missing."
    fi
  fi

  if $target_windows; then
    echo "[windows] Publishing release assets..."
    "$script_dir/windows/publish.sh" "${publish_args[@]}" "$normalized_version" "$effective_release_notes"
  fi
fi

echo ""
echo "Done."
