#!/usr/bin/env bash
set -euo pipefail

# Development environment variable helper.
# Source this file to set CaveViewer environment variables for local runs.
#
# Usage:
#   source env_setup.sh
#   source env_setup.sh --help

print_usage() {
  cat <<'EOF'
Usage:
  source env_setup.sh
  source env_setup.sh --help

Sets CaveViewer development environment variables in the current shell.
EOF
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    -h|--help)
      print_usage
      return 0 2>/dev/null || exit 0
      ;;
    -*)
      echo "Error: unknown option '$1'"
      echo ""
      print_usage
      return 1 2>/dev/null || exit 1
      ;;
    *)
      echo "Error: positional arguments are not supported: '$1'"
      echo ""
      print_usage
      return 1 2>/dev/null || exit 1
      ;;
  esac
done

# Set environment variables if needed
export CAVEVIEWER_HOME="$(pwd)"
export PYTHONPATH="$CAVEVIEWER_HOME"

# Optional: configure update checks.
# Explicit manifest URL (highest priority):
# export CAVEVIEWER_UPDATE_MANIFEST_URL="https://raw.githubusercontent.com/KernalPanic/CaveViewerPlus/main/updates/macos/stable.json"
# Or set a repo/branch and let the app derive the platform-specific manifest URL:
export CAVEVIEWER_GITHUB_REPO="KernalPanic/CaveViewerPlus"
# export CAVEVIEWER_UPDATE_BRANCH="feature/pubkey"
# export CAVEVIEWER_UPDATE_CHANNEL="prerelease"

# You can add additional environment configurations here as necessary

echo "CaveViewer environment setup complete."
