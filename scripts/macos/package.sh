#!/usr/bin/env bash
set -euo pipefail

# macOS package wrapper.
# Builds the macOS app bundle and packages it into a versioned DMG.
#
# Usage:
#   ./scripts/macos/package.sh [--base-download-url=<url>]
#
# Example:
#   ./scripts/macos/package.sh
#   ./scripts/macos/package.sh --base-download-url="https://github.com/owner/CaveViewerPlus/releases/download/v1.2.3"

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

print_usage() {
  cat <<'EOF'
Usage:
  package.sh [--base-download-url=<url>]
  package.sh --help

Builds the macOS app bundle and packages it as a DMG.
EOF
}

base_download_url=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --base-download-url=*)
      base_download_url="${1#--base-download-url=}"
      shift
      ;;
    --base-download-url)
      shift
      if [ "$#" -eq 0 ]; then
        echo "Error: --base-download-url requires a value."
        exit 1
      fi
      base_download_url="$1"
      shift
      ;;
    -h|--help)
      print_usage
      exit 0
      ;;
    -*)
      echo "Error: unknown option '$1'"
      echo ""
      print_usage
      exit 1
      ;;
    *)
      echo "Error: positional arguments are not supported: '$1'"
      echo "Use --base-download-url=<url>."
      exit 1
      ;;
  esac
done

"$script_dir/build.sh"
"$script_dir/package_macos_dmg.sh" --base-download-url "$base_download_url"
