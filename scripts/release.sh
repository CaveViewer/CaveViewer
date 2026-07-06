#!/usr/bin/env bash
set -euo pipefail

# Cross-platform release dispatcher.
#
# Usage:
#   ./scripts/release.sh <target> [args...]
#
# Targets:
#   all-package         -> scripts/all_package.sh
#   macos-package       -> scripts/macos/package.sh
#   macos-publish       -> scripts/macos/publish.sh
#   macos-dist-layout   -> scripts/macos/show_dist_layout.sh
#   windows-package     -> scripts/windows/package.sh
#   windows-publish     -> scripts/windows/publish.sh
#   linux-package       -> scripts/linux/common/package.sh
#   linux-arm64-publish -> scripts/linux/arm64/publish.sh
#   linux-x86_64-publish -> scripts/linux/x86_64/publish.sh

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [ "$#" -lt 1 ]; then
  echo "Usage: $0 <target> [args...]"
  echo "Try: $0 macos-package"
  exit 1
fi

target="$1"
shift

case "$target" in
  all-package)
    exec "$script_dir/all_package.sh" "$@"
    ;;
  macos-package)
    exec "$script_dir/macos/package.sh" "$@"
    ;;
  macos-publish)
    exec "$script_dir/macos/publish.sh" "$@"
    ;;
  macos-dist-layout)
    exec "$script_dir/macos/show_dist_layout.sh" "$@"
    ;;
  windows-package)
    exec "$script_dir/windows/package.sh" "$@"
    ;;
  windows-publish)
    exec "$script_dir/windows/publish.sh" "$@"
    ;;
  linux-package)
    exec "$script_dir/linux/common/package.sh" "$@"
    ;;
  linux-arm64-publish)
    exec "$script_dir/linux/arm64/publish.sh" "$@"
    ;;
  linux-x86_64-publish|linux-amd64-publish)
    exec "$script_dir/linux/x86_64/publish.sh" "$@"
    ;;
  *)
    echo "Error: unknown target '$target'"
    exit 1
    ;;
esac
