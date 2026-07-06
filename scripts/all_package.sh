#!/usr/bin/env bash
set -euo pipefail

# Build package artifacts for all supported platforms from one entrypoint.
#
# Usage:
#   ./scripts/all_package.sh --version=X.Y.Z [--linux-arch=arm64|x86_64|both] [--linux-build=auto|native|docker] [--rebuild] [--skip=macos,linux-arm64,linux-x86_64,windows] [--publish] [--release-notes="..."]
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
linux_build="auto"
rebuild=false
publish=false
release_notes=""
skip_macos=false
skip_linux=false
skip_linux_arm64=false
skip_linux_x86_64=false
skip_windows=false

linux_artifact_exists_for_arch() {
  local arch="$1"
  local suffix="" arch_dir=""
  case "$arch" in
    arm64)
      suffix="aarch64"
      arch_dir="arm64"
      ;;
    amd64)
      suffix="x86_64"
      arch_dir="x86_64"
      ;;
    *) return 1 ;;
  esac
  [ -f "$repo_root/dist/linux/$arch_dir/packages/CaveViewer-${normalized_version}-${suffix}.AppImage" ]
}

linux_arch_is_selected() {
  local arch="$1"
  case "$linux_arch" in
    both) ;;
    arm64) [ "$arch" = "arm64" ] || return 1 ;;
    amd64) [ "$arch" = "amd64" ] || return 1 ;;
    *) return 1 ;;
  esac

  case "$arch" in
    arm64) ! $skip_linux_arm64 ;;
    amd64) ! $skip_linux_x86_64 ;;
    *) return 1 ;;
  esac
}

effective_linux_arch() {
  local want_arm64=false
  local want_amd64=false
  linux_arch_is_selected arm64 && want_arm64=true
  linux_arch_is_selected amd64 && want_amd64=true

  if $want_arm64 && $want_amd64; then
    echo "both"
  elif $want_arm64; then
    echo "arm64"
  elif $want_amd64; then
    echo "amd64"
  else
    echo "none"
  fi
}

linux_arch_display_name() {
  case "$1" in
    amd64) echo "x86_64" ;;
    *) echo "$1" ;;
  esac
}

linux_artifacts_ready() {
  local selected_linux_arch="$1"
  if $run_linux_docker; then
    case "$selected_linux_arch" in
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
    local target_arch="$selected_linux_arch"
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
  --linux-arch=arm64|x86_64|both   Linux build architecture(s). Default: both. amd64 is accepted as an alias for x86_64.
  --linux-build=auto|native|docker  Linux build mode. Default: auto
  --rebuild                         Force Docker Linux image rebuild
  --publish                         After build succeeds, publish artifacts to GitHub via platform publish scripts
  --release-notes="text"            Release notes used when --publish is set (default: "Release X.Y.Z")
  --skip=macos,linux-arm64,linux-x86_64,windows
                                    Comma-separated targets to skip. "linux" skips both Linux architectures.
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
      [ "$linux_arch" = "x86_64" ] && linux_arch="amd64"
      shift
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
    --skip=*)
      skip_list="${1#--skip=}"
      old_ifs="$IFS"
      IFS=','
      for item in $skip_list; do
        IFS="$old_ifs"
        case "${item// /}" in
          macos) skip_macos=true ;;
          linux)
            skip_linux=true
            skip_linux_arm64=true
            skip_linux_x86_64=true
            ;;
          linux-arm64|linux-aarch64) skip_linux_arm64=true ;;
          linux-x86_64|linux-amd64) skip_linux_x86_64=true ;;
          windows) skip_windows=true ;;
          "") ;;
          *)
            echo "Error: unknown skip target '$item'"
            exit 1
            ;;
        esac
        IFS=','
      done
      IFS="$old_ifs"
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
    echo "Error: invalid --linux-arch '$linux_arch' (expected arm64|x86_64|both; amd64 is accepted as an alias)"
    exit 1
    ;;
esac
case "$linux_build" in
  auto|native|docker) ;;
  *)
    echo "Error: invalid --linux-build '$linux_build' (expected auto|native|docker)"
    exit 1
    ;;
esac

selected_linux_arch="$(effective_linux_arch)"
if [ "$selected_linux_arch" = "none" ]; then
  skip_linux=true
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
echo "Linux arch requested: $(linux_arch_display_name "$linux_arch")"
echo "Linux arch selected: $(linux_arch_display_name "$selected_linux_arch")"
echo "Linux build mode: $linux_build"
echo "Publish mode: $publish"
echo "====================================================="

run_linux_native=false
run_linux_docker=false
native_arch=""
if $skip_linux; then
  :
elif [ "$host_os" = "Darwin" ]; then
  if [ "$linux_build" = "native" ]; then
    echo "Error: --linux-build=native is not supported on macOS."
    exit 1
  fi
  run_linux_docker=true
elif [ "$host_os" = "Linux" ]; then
  case "$(uname -m)" in
    x86_64) native_arch="amd64" ;;
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
    if [ "$selected_linux_arch" = "both" ] || [ "$selected_linux_arch" != "$native_arch" ]; then
      echo "Error: --linux-build=native cannot build linux arch '$selected_linux_arch' on native '$native_arch' host."
      exit 1
    fi
    run_linux_native=true
  elif [ "$selected_linux_arch" = "both" ] || [ "$selected_linux_arch" != "$native_arch" ]; then
    if command -v docker >/dev/null 2>&1; then
      run_linux_docker=true
    else
      if [ "$selected_linux_arch" = "both" ]; then
        echo "[linux] Docker not found; falling back to native-only build."
        selected_linux_arch="$native_arch"
        run_linux_native=true
      else
        echo "Error: requested linux arch '$selected_linux_arch' on native '$native_arch' host without Docker."
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
  if $publish && ! $rebuild && linux_artifacts_ready "$selected_linux_arch"; then
    echo "[linux] Reusing existing package artifact(s) for version $normalized_version."
  else
    if $run_linux_docker; then
      echo "[linux] Building package(s) via Docker..."
      if $rebuild; then
        "$script_dir/linux/build_linux_in_docker.sh" "--arch=$selected_linux_arch" --rebuild
      else
        "$script_dir/linux/build_linux_in_docker.sh" "--arch=$selected_linux_arch"
      fi
    elif $run_linux_native; then
      echo "[linux] Building package natively..."
      "$script_dir/linux/common/build.sh"
      "$script_dir/linux/common/package.sh"
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
  if linux_arch_is_selected arm64; then
    print_artifact "Linux ARM64 AppImage" "$repo_root/dist/linux/arm64/packages/CaveViewer-${version}-aarch64.AppImage"
  fi
  if linux_arch_is_selected amd64; then
    print_artifact "Linux x86_64 AppImage" "$repo_root/dist/linux/x86_64/packages/CaveViewer-${version}-x86_64.AppImage"
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
        "$script_dir/macos/publish.sh" --skip-build "$normalized_version" "$effective_release_notes"
    else
      echo "[macos] Skipped publish: requires macOS host."
    fi
  else
    echo "[macos] Publish skipped by option."
  fi

  if ! $skip_linux; then
    if $run_linux_docker || $run_linux_native; then
      publish_linux_arch() {
        local arch="$1"
        echo "[linux-$arch] Publishing release assets..."
        CAVEVIEWER_LINUX_UPDATE_ARCH="$arch" \
          "$script_dir/linux/$arch/publish.sh" --skip-build "$normalized_version" "$effective_release_notes"
      }

      case "$selected_linux_arch" in
        both)
          if linux_artifact_exists_for_arch arm64; then
            publish_linux_arch arm64
          fi
          if linux_artifact_exists_for_arch amd64; then
            publish_linux_arch x86_64
          fi
          ;;
        arm64)
          publish_linux_arch arm64
          ;;
        amd64)
          publish_linux_arch x86_64
          ;;
      esac
    else
      echo "[linux] Skipped publish: unsupported host setup."
    fi
  else
    echo "[linux] Publish skipped by option."
  fi

  if ! $skip_windows; then
    echo "[windows] Publishing release assets..."
      "$script_dir/windows/publish.sh" --skip-build "$normalized_version" "$effective_release_notes"
  else
    echo "[windows] Publish skipped by option."
  fi
fi

echo ""
echo "Done."
