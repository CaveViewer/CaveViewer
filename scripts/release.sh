#!/usr/bin/env bash
set -euo pipefail

# Cross-platform release dispatcher.
#
# Usage:
#   release.sh --target=<target> --version=<version> --notes=<notes> --action=<action> [options]

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "$script_dir/.." && pwd)"
source "$script_dir/common/version.sh"
source "$script_dir/common/artifacts.sh"
version_file="$repo_root/src/caveviewer/version.py"

print_help() {
  cat <<'EOF'
Usage:
  release.sh --target=<target> --version=<version> --notes=<notes> --action=<action> [options]
  release.sh [--help]
  release.sh --target=<target> --help

Required arguments:
  --target             Target list: all, macos-15, windows, linux-arm64, linux-x86_64
  --action             One of: build, package, release
  --version            Release version, for example 1.0.60
  --notes              Release notes, quoted if they contain spaces

Actions:
  build                Create an intermediate app bundle
  package              Create a distributable artifact
  release              Publish/upload artifacts and write update manifests

Options:
  --rebuild            Rebuild Linux Docker image when building/packaging Linux targets
  --pre-release        Mark the GitHub release as a prerelease; only valid with --action=release

Examples:
  release.sh --target=linux-arm64 --version=1.0.60 --notes "Alpha." --action=build
  release.sh --target=linux-arm64 --version=1.0.60 --notes "Alpha." --action=package
  release.sh --target=linux-arm64 --version=1.0.60 --notes "Alpha." --action=release
  release.sh --target=linux-arm64 --version=1.0.60 --notes "Alpha." --action=release --pre-release
  release.sh --target=macos-15,linux-arm64 --version=1.0.60 --notes "Alpha." --action=package
  release.sh --target=all --version=1.0.60 --notes "Alpha." --action=release
EOF
}

print_target_help() {
  case "$1" in
    all)
      cat <<'EOF'
Usage:
  release.sh --target=all --version=<version> --notes=<notes> --action=<build|package|release> [--rebuild] [--pre-release]

If all appears in a comma-separated target list, it takes precedence.
EOF
      ;;
    macos-15)
      echo "Usage: release.sh --target=macos-15 --version=<version> --notes=<notes> --action=<build|package|release> [--pre-release]"
      ;;
    windows)
      echo "Usage: release.sh --target=windows --version=<version> --notes=<notes> --action=<build|package|release> [--pre-release]"
      ;;
    linux-arm64)
      echo "Usage: release.sh --target=linux-arm64 --version=<version> --notes=<notes> --action=<build|package|release> [--rebuild] [--pre-release]"
      ;;
    linux-x86_64)
      echo "Usage: release.sh --target=linux-x86_64 --version=<version> --notes=<notes> --action=<build|package|release> [--rebuild] [--pre-release]"
      ;;
    *)
      echo "Error: unknown target '$1'"
      echo "Expected one of: all, macos-15, windows, linux-arm64, linux-x86_64"
      exit 1
      ;;
  esac
}

is_known_target() {
  case "$1" in
    all|macos-15|windows|linux-arm64|linux-x86_64)
      return 0
      ;;
    *)
      return 1
      ;;
  esac
}

is_known_action() {
  case "$1" in
    build|package|release)
      return 0
      ;;
    *)
      return 1
      ;;
  esac
}

trim_leading_whitespace() {
  local value="$1"
  while [[ "$value" == [[:space:]]* ]]; do
    value="${value#?}"
  done
  printf '%s' "$value"
}

add_target_selection() {
  local selected="$1"
  case "$selected" in
    all)
      selected_all=true
      selected_macos=true
      selected_windows=true
      selected_linux_arm64=true
      selected_linux_x86=true
      ;;
    macos-15)
      selected_macos=true
      ;;
    windows)
      selected_windows=true
      ;;
    linux-arm64)
      selected_linux_arm64=true
      ;;
    linux-x86_64)
      selected_linux_x86=true
      ;;
    "")
      ;;
    *)
      echo "Error: unknown --target entry '$selected'"
      echo "Expected one of: all, macos-15, windows, linux-arm64, linux-x86_64"
      exit 1
      ;;
  esac
}

parse_target_selection() {
  local target_arg="$1"
  local old_ifs="$IFS"
  selected_all=false
  selected_macos=false
  selected_windows=false
  selected_linux_arm64=false
  selected_linux_x86=false

  IFS=','
  for item in $target_arg; do
    IFS="$old_ifs"
    add_target_selection "${item// /}"
    IFS=','
  done
  IFS="$old_ifs"

  if $selected_all; then
    selected_macos=true
    selected_windows=true
    selected_linux_arm64=true
    selected_linux_x86=true
  fi
}

canonical_single_target() {
  if $selected_all; then
    echo "all"
  elif $selected_macos && ! $selected_windows && ! $selected_linux_arm64 && ! $selected_linux_x86; then
    echo "macos-15"
  elif $selected_windows && ! $selected_macos && ! $selected_linux_arm64 && ! $selected_linux_x86; then
    echo "windows"
  elif $selected_linux_arm64 && ! $selected_macos && ! $selected_windows && ! $selected_linux_x86; then
    echo "linux-arm64"
  elif $selected_linux_x86 && ! $selected_macos && ! $selected_windows && ! $selected_linux_arm64; then
    echo "linux-x86_64"
  else
    echo "multi"
  fi
}

selected_target_summary() {
  local targets=()
  $selected_macos && targets+=("macos-15")
  $selected_linux_arm64 && targets+=("linux-arm64")
  $selected_linux_x86 && targets+=("linux-x86_64")
  $selected_windows && targets+=("windows")

  if [ "${#targets[@]}" -eq 0 ]; then
    echo "none"
  else
    local old_ifs="$IFS"
    IFS=','
    echo "${targets[*]}"
    IFS="$old_ifs"
  fi
}

has_linux_target() {
  $selected_linux_arm64 || $selected_linux_x86
}

require_macos_host() {
  if [ "$(uname -s)" != "Darwin" ]; then
    echo "Error: target macos-15 requires a macOS host."
    exit 1
  fi
}

selected_linux_arch() {
  if $selected_linux_arm64 && $selected_linux_x86; then
    echo "both"
  elif $selected_linux_arm64; then
    echo "arm64"
  elif $selected_linux_x86; then
    echo "x86_64"
  else
    echo "none"
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

run_selected_builds() {
  local host_os
  host_os="$(uname -s)"

  if $selected_macos; then
    if [ "$host_os" = "Darwin" ]; then
      "$script_dir/macos/build.sh"
    else
      echo "[macos-15] Skipped: requires macOS host."
    fi
  fi

  $selected_windows && "$script_dir/windows/build.sh"
  $selected_linux_arm64 && "$script_dir/linux/arm64/build.sh" ${passthrough_args[@]+"${passthrough_args[@]}"}
  $selected_linux_x86 && "$script_dir/linux/x86_64/build.sh" ${passthrough_args[@]+"${passthrough_args[@]}"}
}

run_selected_packages() {
  local host_os linux_arch
  host_os="$(uname -s)"
  linux_arch="$(selected_linux_arch)"

  if [ "$(selected_target_summary)" = "none" ]; then
    echo "Error: no release targets selected."
    exit 1
  fi

  if [ "${#multi_target_unknown_args[@]}" -gt 0 ]; then
    echo "Error: unsupported option for multi-target package/release: '${multi_target_unknown_args[0]}'"
    exit 1
  fi

  echo "====================================================="
  echo "CaveViewer packaging"
  echo "Host OS: $host_os"
  echo "Targets: $(selected_target_summary)"
  echo "Linux build mode: docker"
  echo "====================================================="

  if has_linux_target; then
    if ! command -v docker >/dev/null 2>&1; then
      echo "Error: Docker is required for Linux release builds."
      exit 1
    fi
  fi

  if $selected_macos; then
    if [ "$host_os" = "Darwin" ]; then
      local macos_dmg_path="$repo_root/dist/macos/packages/CaveViewer-${normalized_version}.dmg"
      if $reuse_existing_artifacts && ! $rebuild && [ -f "$macos_dmg_path" ]; then
        echo "[macos-15] Reusing existing package: $macos_dmg_path"
      else
        echo "[macos-15] Building package..."
        "$script_dir/macos/package.sh"
      fi
    else
      echo "[macos-15] Skipped: requires macOS host."
    fi
  fi

  if has_linux_target; then
    if $reuse_existing_artifacts && ! $rebuild && linux_artifacts_ready "$linux_arch"; then
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

  if $selected_windows; then
    local windows_zip_path="$repo_root/dist/windows/packages/CaveViewer-${normalized_version}-windows.zip"
    if $reuse_existing_artifacts && ! $rebuild && [ -f "$windows_zip_path" ]; then
      echo "[windows] Reusing existing package: $windows_zip_path"
    else
      echo "[windows] Building package..."
      "$script_dir/windows/package.sh"
    fi
  fi

  echo ""
  echo "====================================================="
  echo "Artifact summary (version $normalized_version)"
  echo "====================================================="

  if $selected_macos && [ "$host_os" = "Darwin" ]; then
    print_artifact "macOS 15 DMG" "$repo_root/dist/macos/packages/CaveViewer-${normalized_version}.dmg"
  fi

  if $selected_linux_arm64; then
    print_artifact "Linux ARM64 AppImage" "$repo_root/dist/linux/arm64/packages/CaveViewer-${normalized_version}-aarch64.AppImage"
  fi

  if $selected_linux_x86; then
    print_artifact "Linux x86_64 AppImage" "$repo_root/dist/linux/x86_64/packages/CaveViewer-${normalized_version}-x86_64.AppImage"
  fi

  if $selected_windows; then
    print_artifact "Windows ZIP" "$repo_root/dist/windows/packages/CaveViewer-${normalized_version}-windows.zip"
  fi
}

run_selected_releases() {
  local host_os publish_args=()
  host_os="$(uname -s)"

  reuse_existing_artifacts=true
  run_selected_packages

  publish_args+=(--use-existing-artifacts)
  $pre_release && publish_args+=(--pre-release)

  echo ""
  echo "====================================================="
  echo "Publishing artifacts"
  echo "====================================================="

  if $selected_macos; then
    if [ "$host_os" = "Darwin" ]; then
      echo "[macos-15] Publishing release assets..."
      "$script_dir/macos/publish.sh" --version "$normalized_version" --notes "$notes" ${publish_args[@]+"${publish_args[@]}"}
    else
      echo "[macos-15] Skipped publish: requires macOS host."
    fi
  fi

  if $selected_linux_arm64; then
    if linux_artifact_exists_for_arch arm64; then
      echo "[linux-arm64] Publishing release assets..."
      "$script_dir/linux/arm64/publish.sh" --version "$normalized_version" --notes "$notes" ${publish_args[@]+"${publish_args[@]}"}
    else
      echo "[linux-arm64] Publish skipped: artifact missing."
    fi
  fi

  if $selected_linux_x86; then
    if linux_artifact_exists_for_arch x86_64; then
      echo "[linux-x86_64] Publishing release assets..."
      "$script_dir/linux/x86_64/publish.sh" --version "$normalized_version" --notes "$notes" ${publish_args[@]+"${publish_args[@]}"}
    else
      echo "[linux-x86_64] Publish skipped: artifact missing."
    fi
  fi

  if $selected_windows; then
    echo "[windows] Publishing release assets..."
    "$script_dir/windows/publish.sh" --version "$normalized_version" --notes "$notes" ${publish_args[@]+"${publish_args[@]}"}
  fi
}

target=""
action=""
version=""
notes=""
show_help=false
pre_release=false
rebuild=false
reuse_existing_artifacts=false
passthrough_args=()
multi_target_unknown_args=()

if [ "$#" -eq 0 ]; then
  print_help
  exit 1
fi

while [ "$#" -gt 0 ]; do
  arg="$(trim_leading_whitespace "$1")"
  case "$arg" in
    -h|--help)
      show_help=true
      shift
      ;;
    --target=*)
      target="${arg#--target=}"
      shift
      ;;
    --target)
      shift
      if [ "$#" -eq 0 ]; then
        echo "Error: --target requires a value."
        exit 1
      fi
      target="$(trim_leading_whitespace "$1")"
      shift
      ;;
    --action=*)
      action="${arg#--action=}"
      shift
      ;;
    --action)
      shift
      if [ "$#" -eq 0 ]; then
        echo "Error: --action requires a value."
        exit 1
      fi
      action="$(trim_leading_whitespace "$1")"
      shift
      ;;
    --version=*)
      version="${arg#--version=}"
      shift
      ;;
    --version)
      shift
      if [ "$#" -eq 0 ]; then
        echo "Error: --version requires a value."
        exit 1
      fi
      version="$(trim_leading_whitespace "$1")"
      shift
      ;;
    --notes=*)
      notes="${arg#--notes=}"
      shift
      ;;
    --notes)
      shift
      if [ "$#" -eq 0 ]; then
        echo "Error: --notes requires a value."
        exit 1
      fi
      notes="$(trim_leading_whitespace "$1")"
      shift
      ;;
    --targets|--targets=*)
      echo "Error: unknown option '$arg'"
      exit 1
      ;;
    --rebuild)
      rebuild=true
      passthrough_args+=("$arg")
      shift
      ;;
    --pre-release)
      pre_release=true
      shift
      ;;
    --)
      shift
      while [ "$#" -gt 0 ]; do
        passthrough_args+=("$1")
        multi_target_unknown_args+=("$1")
        shift
      done
      ;;
    --*)
      passthrough_args+=("$arg")
      multi_target_unknown_args+=("$arg")
      shift
      ;;
    *)
      echo "Error: positional arguments are not supported: '$arg'"
      echo "Use explicit options, for example: release.sh --target=linux-arm64 --version=1.0.60 --notes \"Alpha.\" --action=build"
      exit 1
      ;;
  esac
done

if $show_help; then
  if [ -n "$target" ]; then
    first_help_target="${target%%,*}"
    print_target_help "$first_help_target"
  else
    print_help
  fi
  exit 0
fi

if [ -z "$target" ] || [ -z "$action" ] || [ -z "$version" ] || [ -z "$notes" ]; then
  echo "Error: target, action, version, and notes are required."
  echo ""
  print_help
  exit 1
fi

if ! is_known_target "$target"; then
  parse_target_selection "$target"
fi

if ! is_known_action "$action"; then
  echo "Error: unknown action '$action'"
  echo "Expected one of: build, package, release"
  exit 1
fi

if $pre_release && [ "$action" != "release" ]; then
  echo "Error: --pre-release is only valid with --action=release."
  exit 1
fi

parse_target_selection "$target"

if $rebuild && ! has_linux_target; then
  echo "Error: --rebuild is only valid when a Linux target is selected."
  exit 1
fi

pre_release_args=()
if $pre_release; then
  pre_release_args+=(--pre-release)
fi

normalized_version="${version#v}"
if [ -z "$normalized_version" ]; then
  echo "Error: version cannot be empty."
  exit 1
fi

case "$normalized_version" in
  -*|--*)
    echo "Error: version looks like an option: '$version'"
    echo "Use --version=<version> or --version <version>."
    exit 1
    ;;
esac

case "$notes" in
  "" )
    echo "Error: notes cannot be empty."
    exit 1
    ;;
  -*|--*)
    echo "Error: notes looks like an option: '$notes'"
    echo "Use --notes=\"Release notes\" or --notes \"Release notes\"."
    exit 1
    ;;
esac

if [ "$action" = "release" ] && [ -z "${CAVEVIEWER_RELEASE_SIGNING_PRIVATE_KEY:-}" ]; then
  echo "Error: CAVEVIEWER_RELEASE_SIGNING_PRIVATE_KEY must be set when --action=release."
  exit 1
fi

if [ ! -f "$version_file" ]; then
  echo "Error: version file not found: $version_file"
  exit 1
fi

current_version="$(cv_read_app_version "$version_file")"
if [ "$current_version" != "$normalized_version" ]; then
  cv_set_app_version "$version_file" "$normalized_version"
  echo "Set APP_VERSION: $current_version -> $normalized_version"
fi

set +u
dispatch_target="$(canonical_single_target)"

case "$dispatch_target:$action" in
  all:build)
    run_selected_builds
    ;;
  all:package)
    run_selected_packages
    ;;
  all:release)
    run_selected_releases
    ;;
  macos-15:build)
    require_macos_host
    exec "$script_dir/macos/build.sh" ${passthrough_args[@]+"${passthrough_args[@]}"}
    ;;
  macos-15:package)
    require_macos_host
    exec "$script_dir/macos/package.sh" ${passthrough_args[@]+"${passthrough_args[@]}"}
    ;;
  macos-15:release)
    require_macos_host
    exec "$script_dir/macos/publish.sh" --version "$normalized_version" --notes "$notes" ${pre_release_args[@]+"${pre_release_args[@]}"} ${passthrough_args[@]+"${passthrough_args[@]}"}
    ;;
  windows:build)
    exec "$script_dir/windows/build.sh" ${passthrough_args[@]+"${passthrough_args[@]}"}
    ;;
  windows:package)
    exec "$script_dir/windows/package.sh" ${passthrough_args[@]+"${passthrough_args[@]}"}
    ;;
  windows:release)
    exec "$script_dir/windows/publish.sh" --version "$normalized_version" --notes "$notes" ${pre_release_args[@]+"${pre_release_args[@]}"} ${passthrough_args[@]+"${passthrough_args[@]}"}
    ;;
  linux-arm64:build)
    exec "$script_dir/linux/arm64/build.sh" ${passthrough_args[@]+"${passthrough_args[@]}"}
    ;;
  linux-arm64:package)
    exec "$script_dir/linux/arm64/package.sh" ${passthrough_args[@]+"${passthrough_args[@]}"}
    ;;
  linux-arm64:release)
    exec "$script_dir/linux/arm64/publish.sh" --version "$normalized_version" --notes "$notes" ${pre_release_args[@]+"${pre_release_args[@]}"} ${passthrough_args[@]+"${passthrough_args[@]}"}
    ;;
  linux-x86_64:build)
    exec "$script_dir/linux/x86_64/build.sh" ${passthrough_args[@]+"${passthrough_args[@]}"}
    ;;
  linux-x86_64:package)
    exec "$script_dir/linux/x86_64/package.sh" ${passthrough_args[@]+"${passthrough_args[@]}"}
    ;;
  linux-x86_64:release)
    exec "$script_dir/linux/x86_64/publish.sh" --version "$normalized_version" --notes "$notes" ${pre_release_args[@]+"${pre_release_args[@]}"} ${passthrough_args[@]+"${passthrough_args[@]}"}
    ;;
  multi:build)
    run_selected_builds
    ;;
  multi:package)
    run_selected_packages
    ;;
  multi:release)
    run_selected_releases
    ;;
  *)
    echo "Error: unsupported target/action combination: $target/$action"
    exit 1
    ;;
esac
