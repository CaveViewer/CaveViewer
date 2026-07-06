#!/usr/bin/env bash
set -euo pipefail

# Build package artifacts for explicitly selected release targets.
#
# Usage:
#   ./scripts/all_package.sh --version=X.Y.Z [--targets=macos,linux-arm64,linux-x86_64,windows] [--linux-build=auto|native|docker] [--rebuild] [--publish] [--release-notes="..."]
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
linux_build="auto"
rebuild=false
publish=false
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
  --linux-build=auto|native|docker  Linux build mode. Default: auto
  --rebuild                         Force Linux Docker image rebuild
  --publish                         After build succeeds, publish artifacts to GitHub via target publish scripts
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
    --linux-build=*)
      linux_build="${1#--linux-build=}"
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

case "$linux_build" in
  auto|native|docker) ;;
  *)
    echo "Error: invalid --linux-build '$linux_build' (expected auto|native|docker)"
    exit 1
    ;;
esac

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
echo "Linux build mode: $linux_build"
echo "Publish mode: $publish"
echo "====================================================="

run_linux_native=false
run_linux_docker=false
native_arch=""
if has_linux_target; then
  if [ "$host_os" = "Darwin" ]; then
    if [ "$linux_build" = "native" ]; then
      echo "Error: --linux-build=native is not supported on macOS."
      exit 1
    fi
    run_linux_docker=true
  elif [ "$host_os" = "Linux" ]; then
    case "$(uname -m)" in
      x86_64) native_arch="x86_64" ;;
      aarch64) native_arch="arm64" ;;
    esac

    if [ "$linux_build" = "docker" ]; then
      if command -v docker >/dev/null 2>&1; then
        run_linux_docker=true
      else
        echo "Error: --linux-build=docker requested, but Docker is not installed or not in PATH."
        exit 1
      fi
    elif [ "$linux_build" = "native" ]; then
      if [ "$linux_arch" = "both" ] || [ "$linux_arch" != "$native_arch" ]; then
        echo "Error: --linux-build=native cannot build linux target '$linux_arch' on native '$native_arch' host."
        exit 1
      fi
      run_linux_native=true
    elif [ "$linux_arch" = "both" ] || [ "$linux_arch" != "$native_arch" ]; then
      if command -v docker >/dev/null 2>&1; then
        run_linux_docker=true
      else
        if [ "$linux_arch" = "both" ]; then
          echo "[linux] Docker not found; falling back to native-only build for $native_arch."
          linux_arch="$native_arch"
          if [ "$native_arch" = "arm64" ]; then
            target_linux_x86_64=false
          else
            target_linux_arm64=false
          fi
          run_linux_native=true
        else
          echo "Error: requested linux target '$linux_arch' on native '$native_arch' host without Docker."
          exit 1
        fi
      fi
    else
      run_linux_native=true
    fi
  else
    echo "[linux] Unsupported host for Linux packaging: $host_os"
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
    if $run_linux_docker; then
      echo "[linux] Building package(s) via Docker..."
      if $rebuild; then
        "$script_dir/linux/build_linux_in_docker.sh" "--arch=$linux_arch" --rebuild
      else
        "$script_dir/linux/build_linux_in_docker.sh" "--arch=$linux_arch"
      fi
    elif $run_linux_native; then
      echo "[linux] Building package natively..."
      "$script_dir/linux/common/build.sh"
      "$script_dir/linux/common/package.sh"
    else
      echo "[linux] Skipped: unsupported host setup."
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

  if $target_macos; then
    if [ "$host_os" = "Darwin" ]; then
      echo "[macos] Publishing release assets..."
      "$script_dir/macos/publish.sh" --skip-build "$normalized_version" "$effective_release_notes"
    else
      echo "[macos] Skipped publish: requires macOS host."
    fi
  fi

  if $target_linux_arm64; then
    if $run_linux_docker || $run_linux_native; then
      if linux_artifact_exists_for_arch arm64; then
        echo "[linux-arm64] Publishing release assets..."
        "$script_dir/linux/arm64/publish.sh" --skip-build "$normalized_version" "$effective_release_notes"
      else
        echo "[linux-arm64] Publish skipped: artifact missing."
      fi
    else
      echo "[linux-arm64] Skipped publish: unsupported host setup."
    fi
  fi

  if $target_linux_x86_64; then
    if $run_linux_docker || $run_linux_native; then
      if linux_artifact_exists_for_arch x86_64; then
        echo "[linux-x86_64] Publishing release assets..."
        "$script_dir/linux/x86_64/publish.sh" --skip-build "$normalized_version" "$effective_release_notes"
      else
        echo "[linux-x86_64] Publish skipped: artifact missing."
      fi
    else
      echo "[linux-x86_64] Skipped publish: unsupported host setup."
    fi
  fi

  if $target_windows; then
    echo "[windows] Publishing release assets..."
    "$script_dir/windows/publish.sh" --skip-build "$normalized_version" "$effective_release_notes"
  fi
fi

echo ""
echo "Done."
