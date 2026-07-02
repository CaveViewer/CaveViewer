#!/usr/bin/env bash
set -euo pipefail

# Build the Linux app inside a Docker container so a Linux machine is not
# required. Produces dist/linux/packages/ artifacts on the host, which
# linux/publish_release.sh then uploads when run from macOS.
#
# Usage:
#   ./scripts/linux/build_linux_in_docker.sh [--arch=arm64|amd64|both] [--rebuild]
#
# Default builds BOTH architectures. arm64 runs natively on Apple Silicon;
# amd64 runs under QEMU (slower). Each arch gets its own Docker image tag so
# switching between them doesn't force a full image rebuild.

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "$script_dir/../.." && pwd)"
dockerfile="$script_dir/Dockerfile.linux-build"

if ! command -v docker >/dev/null 2>&1; then
  echo "Error: docker is not installed or not in PATH."
  exit 1
fi

rebuild=false
archs=("arm64" "amd64")  # default: build both

for arg in "$@"; do
  case "$arg" in
    --rebuild) rebuild=true ;;
    --arch=both) archs=("arm64" "amd64") ;;
    --arch=*) archs=("${arg#--arch=}") ;;
  esac
done

for arch in "${archs[@]}"; do
  platform="linux/$arch"
  image_name="caveviewer-linux-build-$arch"

  echo ""
  echo "==== Building for $platform ===="

  if $rebuild || ! docker image inspect "$image_name" >/dev/null 2>&1; then
    echo "Building Docker image ($image_name)..."
    build_args=(--platform "$platform" --build-arg "BUILD_PLATFORM=$platform" -f "$dockerfile" -t "$image_name")
    if $rebuild; then
      build_args=(--no-cache "${build_args[@]}")
      if [ -d "$repo_root/.venv" ]; then
        echo "Removing stale .venv..."
        rm -rf "$repo_root/.venv"
      fi
    fi
    DOCKER_BUILDKIT=1 docker build "${build_args[@]}" "$repo_root"
  else
    echo "Using cached Docker image ($image_name). Pass --rebuild to force a rebuild."
  fi

  echo ""
  echo "Running $arch build in Docker..."
  docker run --rm \
    --platform "$platform" \
    -v "$repo_root:/workspace" \
    -w /workspace \
    "$image_name" \
    bash -c "./scripts/linux/build_linux_app.sh && ./scripts/linux/package.sh"
done

echo ""
echo "Done. Artifacts:"
ls -lh "$repo_root/dist/linux/packages/" 2>/dev/null || echo "  (dist/linux/packages/ not found -- check build output above)"
