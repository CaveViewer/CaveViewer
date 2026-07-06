#!/usr/bin/env bash
set -euo pipefail

# Thin build target for the Windows release pipeline.
# Today this delegates to package.sh, which creates the Windows-ready source
# bundle and metadata. Keep this wrapper as the stable entry point for the
# future installer build.

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

print_usage() {
  cat <<'EOF'
Usage:
  build.sh [base_download_url]
  build.sh --help

Builds the Windows release bundle. This currently delegates to package.sh.
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

exec "$script_dir/package.sh" "$@"
