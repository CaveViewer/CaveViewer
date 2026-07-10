#!/usr/bin/env bash
set -euo pipefail

# ARM64 Linux build wrapper.
# Runs the Docker-backed Linux build driver for the ARM64/aarch64 target.
# Prefer the top-level release.sh dispatcher for normal release work.

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "$script_dir/../../.." && pwd)"

print_usage() {
  cat <<'EOF'
Usage:
  build.sh [--rebuild]
  build.sh --help

Builds the Linux ARM64 app bundle through Docker.
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

exec "$repo_root/scripts/linux/build_linux_in_docker.sh" --arch=arm64 --step=build "$@"
