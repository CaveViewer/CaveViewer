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
linux_build_venv_template="${CAVEVIEWER_LINUX_BUILD_VENV:-$repo_root/.venv-linux-build-{arch}}"

dockerfile_hash=""
if command -v shasum >/dev/null 2>&1; then
  dockerfile_hash="$(shasum -a 256 "$dockerfile" | awk '{print $1}')"
elif command -v sha256sum >/dev/null 2>&1; then
  dockerfile_hash="$(sha256sum "$dockerfile" | awk '{print $1}')"
fi

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

arch_count="${#archs[@]}"

for arch in "${archs[@]}"; do
  platform="linux/$arch"
  image_name="caveviewer-linux-build-$arch"

  if [[ "$linux_build_venv_template" == *"{arch}"* ]]; then
    linux_build_venv="${linux_build_venv_template//\{arch\}/$arch}"
  elif [ "$arch_count" -gt 1 ]; then
    linux_build_venv="${linux_build_venv_template}-$arch"
  else
    linux_build_venv="$linux_build_venv_template"
  fi

  if [[ "$linux_build_venv" == "$repo_root"/* ]]; then
    linux_build_venv_in_container="/workspace/${linux_build_venv#"$repo_root"/}"
  elif [[ "$linux_build_venv" == "$repo_root" ]]; then
    linux_build_venv_in_container="/workspace"
  else
    linux_build_venv_in_container="$linux_build_venv"
  fi

  echo ""
  echo "==== Building for $platform ===="

  needs_build=false
  dockerfile_changed=false

  if ! docker image inspect "$image_name" >/dev/null 2>&1; then
    needs_build=true
  elif [ -n "$dockerfile_hash" ]; then
    image_hash="$(docker image inspect -f '{{ index .Config.Labels "caveviewer.dockerfile_hash" }}' "$image_name" 2>/dev/null || true)"
    if [ "$image_hash" != "$dockerfile_hash" ]; then
      needs_build=true
      dockerfile_changed=true
    fi
  fi

  if $rebuild || $needs_build; then
    if $rebuild; then
      echo "Rebuild requested."
    elif $dockerfile_changed; then
      echo "Dockerfile changed; rebuilding $image_name to refresh build environment."
    fi
    echo "Building Docker image ($image_name)..."
    build_args=(--platform "$platform" --build-arg "BUILD_PLATFORM=$platform" -f "$dockerfile" -t "$image_name")
    if [ -n "$dockerfile_hash" ]; then
      build_args+=(--label "caveviewer.dockerfile_hash=$dockerfile_hash")
    fi
    if $rebuild; then
      build_args=(--no-cache "${build_args[@]}")
      if [ -d "$linux_build_venv" ]; then
        echo "Removing stale Linux build venv: $linux_build_venv"
        rm -rf "$linux_build_venv"
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
    -e "CAVEVIEWER_LINUX_BUILD_VENV=$linux_build_venv_in_container" \
    -v "$repo_root:/workspace" \
    -w /workspace \
    "$image_name" \
    bash -c "./scripts/linux/build_linux_app.sh && ./scripts/linux/package.sh"
done

echo ""
echo "Done. Artifacts:"
ls -lh "$repo_root/dist/linux/packages/" 2>/dev/null || echo "  (dist/linux/packages/ not found -- check build output above)"
