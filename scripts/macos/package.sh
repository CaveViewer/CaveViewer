#!/usr/bin/env bash
set -euo pipefail

# Build and package the macOS DMG in one command.
#
# Usage:
#   ./scripts/macos/package.sh [base_download_url]
#
# Example:
#   ./scripts/macos/package.sh
#   ./scripts/macos/package.sh "https://github.com/owner/CaveViewerPlus/releases/download/v1.2.3"

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

print_usage() {
  cat <<'EOF'
Usage:
  package.sh [base_download_url]
  package.sh --help

Builds the macOS app bundle and packages it as a DMG.
EOF
}

if [ "${1:-}" = "-h" ] || [ "${1:-}" = "--help" ]; then
  print_usage
  exit 0
fi

if [ "$#" -gt 1 ]; then
  echo "Error: too many arguments."
  echo ""
  print_usage
  exit 1
fi

base_download_url="${1:-}"

"$script_dir/build.sh"
"$script_dir/package_macos_dmg.sh" "$base_download_url"
