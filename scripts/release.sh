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
#   macos-publish       -> scripts/macos/publish_release.sh
#   macos-dist-layout   -> scripts/macos/show_dist_layout.sh
#   windows-package     -> scripts/windows/package.sh
#   windows-publish     -> scripts/windows/publish_release.sh
#   linux-package       -> scripts/linux/package.sh
#   linux-publish       -> scripts/linux/publish_release.sh

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
    exec "$script_dir/macos/publish_release.sh" "$@"
    ;;
  macos-dist-layout)
    exec "$script_dir/macos/show_dist_layout.sh" "$@"
    ;;
  windows-package)
    exec "$script_dir/windows/package.sh" "$@"
    ;;
  windows-publish)
    exec "$script_dir/windows/publish_release.sh" "$@"
    ;;
  linux-package)
    exec "$script_dir/linux/package.sh" "$@"
    ;;
  linux-publish)
    exec "$script_dir/linux/publish_release.sh" "$@"
    ;;
  *)
    echo "Error: unknown target '$target'"
    exit 1
    ;;
esac
