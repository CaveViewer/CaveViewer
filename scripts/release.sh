#!/usr/bin/env bash
set -euo pipefail

# Cross-platform release dispatcher.
#
# Usage:
#   release.sh --target=<target> --version=<version> --notes=<notes> --action=<action> [options]

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "$script_dir/.." && pwd)"
source "$script_dir/common/version.sh"
version_file="$repo_root/caveviewer_version.py"

print_help() {
  cat <<'EOF'
Usage:
  release.sh --target=<target> --version=<version> --notes=<notes> --action=<action> [options]
  release.sh [--help]
  release.sh --target=<target> --help

Required arguments:
  --target             Target list: all, macos, windows, linux-arm64, linux-x86
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
  release.sh --target=macos,linux-arm64 --version=1.0.60 --notes "Alpha." --action=package
  release.sh --target=all,linux-arm64 --version=1.0.60 --notes "Alpha." --action=release
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
    macos)
      echo "Usage: release.sh --target=macos --version=<version> --notes=<notes> --action=<build|package|release> [--pre-release]"
      ;;
    windows)
      echo "Usage: release.sh --target=windows --version=<version> --notes=<notes> --action=<build|package|release> [--pre-release]"
      ;;
    linux-arm64)
      echo "Usage: release.sh --target=linux-arm64 --version=<version> --notes=<notes> --action=<build|package|release> [--rebuild] [--pre-release]"
      ;;
    linux-x86|linux-x86_64|linux-amd64)
      echo "Usage: release.sh --target=linux-x86 --version=<version> --notes=<notes> --action=<build|package|release> [--rebuild] [--pre-release]"
      ;;
    *)
      echo "Error: unknown target '$1'"
      echo "Expected one of: all, macos, windows, linux-arm64, linux-x86"
      exit 1
      ;;
  esac
}

is_known_target() {
  case "$1" in
    all|macos|windows|linux-arm64|linux-x86|linux-x86_64|linux-amd64)
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

removed_target_message() {
  case "$1" in
    macos-build|macos-package)
      echo "Error: target '$1' was removed."
      echo "Use: release.sh --target=macos --version=<version> --notes=<notes> --action=${1#macos-}"
      return 0
      ;;
    windows-build|windows-package)
      echo "Error: target '$1' was removed."
      echo "Use: release.sh --target=windows --version=<version> --notes=<notes> --action=${1#windows-}"
      return 0
      ;;
    linux-arm64-build|linux-arm64-package)
      echo "Error: target '$1' was removed."
      echo "Use: release.sh --target=linux-arm64 --version=<version> --notes=<notes> --action=${1#linux-arm64-}"
      return 0
      ;;
    linux-x86_64-build|linux-x86_64-package|linux-amd64-build|linux-amd64-package)
      echo "Error: target '$1' was removed."
      local action="${1##*-}"
      echo "Use: release.sh --target=linux-x86 --version=<version> --notes=<notes> --action=$action"
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
    macos)
      selected_macos=true
      ;;
    windows)
      selected_windows=true
      ;;
    linux-arm64|linux-aarch64)
      selected_linux_arm64=true
      ;;
    linux-x86|linux-x86_64|linux-amd64)
      selected_linux_x86=true
      ;;
    "")
      ;;
    *)
      echo "Error: unknown --target entry '$selected'"
      echo "Expected one of: all, macos, windows, linux-arm64, linux-x86"
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
    echo "macos"
  elif $selected_windows && ! $selected_macos && ! $selected_linux_arm64 && ! $selected_linux_x86; then
    echo "windows"
  elif $selected_linux_arm64 && ! $selected_macos && ! $selected_windows && ! $selected_linux_x86; then
    echo "linux-arm64"
  elif $selected_linux_x86 && ! $selected_macos && ! $selected_windows && ! $selected_linux_arm64; then
    echo "linux-x86"
  else
    echo "multi"
  fi
}

selected_targets_for_all_package() {
  if $selected_all; then
    echo "all"
  else
    local targets=()
    $selected_macos && targets+=("macos")
    $selected_windows && targets+=("windows")
    $selected_linux_arm64 && targets+=("linux-arm64")
    $selected_linux_x86 && targets+=("linux-x86_64")
    local old_ifs="$IFS"
    IFS=','
    echo "${targets[*]}"
    IFS="$old_ifs"
  fi
}

target=""
action=""
version=""
notes=""
show_help=false
pre_release=false
passthrough_args=()
all_package_args=()

if [ "$#" -eq 0 ]; then
  print_help
  exit 1
fi

while [ "$#" -gt 0 ]; do
  arg="$(trim_leading_whitespace "$1")"
  case "$arg" in
    -h|--help|help)
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
      echo "Error: --targets was removed."
      echo "Use a comma-separated --target value instead, for example: --target=macos,linux-arm64"
      exit 1
      ;;
    --rebuild)
      passthrough_args+=("$arg")
      all_package_args+=("$arg")
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
        all_package_args+=("$1")
        shift
      done
      ;;
    --*)
      passthrough_args+=("$arg")
      all_package_args+=("$arg")
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

if removed_target_message "$target"; then
  exit 1
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
parse_target_selection "$target"
dispatch_target="$(canonical_single_target)"
all_package_target_list="$(selected_targets_for_all_package)"

case "$dispatch_target:$action" in
  all:build)
    host_os="$(uname -s)"
    if $selected_macos; then
      if [ "$host_os" = "Darwin" ]; then
        "$script_dir/macos/build.sh"
      else
        echo "[macos] Skipped: requires macOS host."
      fi
    fi
    $selected_windows && "$script_dir/windows/build.sh"
    $selected_linux_arm64 && "$script_dir/linux/arm64/build.sh" "${passthrough_args[@]}"
    $selected_linux_x86 && "$script_dir/linux/x86_64/build.sh" "${passthrough_args[@]}"
    ;;
  all:package)
    exec "$script_dir/all_package.sh" --version "$normalized_version" --release-notes "$notes" --targets="$all_package_target_list" "${all_package_args[@]}"
    ;;
  all:release)
    exec "$script_dir/all_package.sh" --version "$normalized_version" --release-notes "$notes" --targets="$all_package_target_list" --publish "${pre_release_args[@]}" "${all_package_args[@]}"
    ;;
  macos:build)
    exec "$script_dir/macos/build.sh" "${passthrough_args[@]}"
    ;;
  macos:package)
    exec "$script_dir/macos/package.sh" "${passthrough_args[@]}"
    ;;
  macos:release)
    exec "$script_dir/macos/publish.sh" "${pre_release_args[@]}" "${passthrough_args[@]}" "$normalized_version" "$notes"
    ;;
  windows:build)
    exec "$script_dir/windows/build.sh" "${passthrough_args[@]}"
    ;;
  windows:package)
    exec "$script_dir/windows/package.sh" "${passthrough_args[@]}"
    ;;
  windows:release)
    exec "$script_dir/windows/publish.sh" "${pre_release_args[@]}" "${passthrough_args[@]}" "$normalized_version" "$notes"
    ;;
  linux-arm64:build)
    exec "$script_dir/linux/arm64/build.sh" "${passthrough_args[@]}"
    ;;
  linux-arm64:package)
    exec "$script_dir/linux/arm64/package.sh" "${passthrough_args[@]}"
    ;;
  linux-arm64:release)
    exec "$script_dir/linux/arm64/publish.sh" "${pre_release_args[@]}" "${passthrough_args[@]}" "$normalized_version" "$notes"
    ;;
  linux-x86:build)
    exec "$script_dir/linux/x86_64/build.sh" "${passthrough_args[@]}"
    ;;
  linux-x86:package)
    exec "$script_dir/linux/x86_64/package.sh" "${passthrough_args[@]}"
    ;;
  linux-x86:release)
    exec "$script_dir/linux/x86_64/publish.sh" "${pre_release_args[@]}" "${passthrough_args[@]}" "$normalized_version" "$notes"
    ;;
  multi:build)
    host_os="$(uname -s)"
    if $selected_macos; then
      if [ "$host_os" = "Darwin" ]; then
        "$script_dir/macos/build.sh"
      else
        echo "[macos] Skipped: requires macOS host."
      fi
    fi
    $selected_windows && "$script_dir/windows/build.sh"
    $selected_linux_arm64 && "$script_dir/linux/arm64/build.sh" "${passthrough_args[@]}"
    $selected_linux_x86 && "$script_dir/linux/x86_64/build.sh" "${passthrough_args[@]}"
    ;;
  multi:package)
    exec "$script_dir/all_package.sh" --version "$normalized_version" --release-notes "$notes" --targets="$all_package_target_list" "${all_package_args[@]}"
    ;;
  multi:release)
    exec "$script_dir/all_package.sh" --version "$normalized_version" --release-notes "$notes" --targets="$all_package_target_list" --publish "${pre_release_args[@]}" "${all_package_args[@]}"
    ;;
  *)
    echo "Error: unsupported target/action combination: $target/$action"
    exit 1
    ;;
esac
