#!/usr/bin/env bash
set -euo pipefail

# x86_64 Linux build wrapper.
# Runs the Docker-backed Linux build driver for the x86_64 target.
# Prefer the top-level scripts/release.sh dispatcher for normal release work.

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "$script_dir/../../.." && pwd)"

print_usage() {
  cat <<'EOF'
Usage:
  build.sh [--rebuild]
  build.sh --help

Builds the Linux x86_64 app bundle through Docker.
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

exec "$repo_root/scripts/linux/build_linux_in_docker.sh" --arch=x86_64 --step=build "$@"
