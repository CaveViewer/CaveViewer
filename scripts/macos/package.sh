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

base_download_url="${1:-}"

"$script_dir/build.sh"
"$script_dir/package_macos_dmg.sh" "$base_download_url"
