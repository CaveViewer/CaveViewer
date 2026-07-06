#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

print_usage() {
  cat <<'EOF'
Usage:
  publish.sh [--skip-build] <version> [release_notes]
  publish.sh --help

Publishes the Linux x86_64 AppImage and update manifest.
EOF
}

if [ "$#" -eq 0 ]; then
  echo "Error: version is required."
  echo ""
  print_usage
  exit 1
fi

if [ "${1:-}" = "-h" ] || [ "${1:-}" = "--help" ]; then
  print_usage
  exit 0
fi

export CAVEVIEWER_LINUX_UPDATE_ARCH=x86_64
exec "$script_dir/../common/publish.sh" "$@"
