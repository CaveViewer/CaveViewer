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
  build.sh [--base-download-url=<url>]
  build.sh --help

Builds the Windows release bundle. This currently delegates to package.sh.
EOF
}

remaining_args=("$@")
while [ "$#" -gt 0 ]; do
  case "$1" in
    --base-download-url=*)
      shift
      ;;
    --base-download-url)
      shift
      if [ "$#" -eq 0 ]; then
        echo "Error: --base-download-url requires a value."
        exit 1
      fi
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

exec "$script_dir/package.sh" "${remaining_args[@]}"
