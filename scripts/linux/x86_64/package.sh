#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "$script_dir/../../.." && pwd)"

print_usage() {
  cat <<'EOF'
Usage:
  package.sh [--rebuild]
  package.sh --help

Packages the Linux x86_64 AppImage through Docker.
EOF
}

if [ "${1:-}" = "-h" ] || [ "${1:-}" = "--help" ]; then
  print_usage
  exit 0
fi

for arg in "$@"; do
  case "$arg" in
    --rebuild) ;;
    -*)
      echo "Error: unknown option '$arg'"
      echo ""
      print_usage
      exit 1
      ;;
    *)
      echo "Error: positional arguments are not supported: '$arg'"
      echo ""
      print_usage
      exit 1
      ;;
  esac
done

exec "$repo_root/scripts/linux/build_linux_in_docker.sh" --arch=x86_64 --step=package "$@"
