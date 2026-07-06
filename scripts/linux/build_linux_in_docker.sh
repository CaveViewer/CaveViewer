#!/usr/bin/env bash
set -euo pipefail

# Build the Linux app inside a Docker container. This is the only supported
# Linux release build path; the common build/package scripts refuse direct
# host execution.
#
# Usage:
#   ./scripts/linux/build_linux_in_docker.sh [--arch=arm64|x86_64|both] [--step=build|package|all] [--rebuild]
#
# Default builds BOTH architectures. arm64 runs without emulation on Apple
# Silicon; x86_64/amd64 runs under QEMU (slower). Each arch gets its own Docker
# image tag so switching between them doesn't force a full image rebuild.

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "$script_dir/../.." && pwd)"
dockerfile="$script_dir/Dockerfile.linux-build"
linux_build_venv_template="${CAVEVIEWER_LINUX_BUILD_VENV:-$repo_root/.venv-linux-build-{arch}}"

print_help() {
  cat <<'EOF'
Usage:
  build_linux_in_docker.sh [--arch=arm64|x86_64|amd64|both] [--step=build|package|all] [--rebuild]

Options:
  --arch=value     Linux architecture to build. Default: both
  --step=value     Build step to run. Default: all
  --rebuild        Rebuild the Docker image and clear the cached build venv
  -h, --help       Show this help
EOF
}

dockerfile_hash=""
if command -v shasum >/dev/null 2>&1; then
  dockerfile_hash="$(shasum -a 256 "$dockerfile" | awk '{print $1}')"
elif command -v sha256sum >/dev/null 2>&1; then
  dockerfile_hash="$(sha256sum "$dockerfile" | awk '{print $1}')"
fi

rebuild=false
archs=("arm64" "amd64")  # default: build both
step="all"

for arg in "$@"; do
  case "$arg" in
    -h|--help)
      print_help
      exit 0
      ;;
    --rebuild) rebuild=true ;;
    --arch=both) archs=("arm64" "amd64") ;;
    --arch=amd64) archs=("amd64") ;;
    --arch=x86_64) archs=("amd64") ;;
    --arch=*) archs=("${arg#--arch=}") ;;
    --step=build|--step=package|--step=all) step="${arg#--step=}" ;;
    --step=*)
      echo "Error: invalid ${arg} (expected --step=build, --step=package, or --step=all)"
      exit 1
      ;;
    -*)
      echo "Error: unknown option '$arg'"
      exit 1
      ;;
  esac
done

for arch in "${archs[@]}"; do
  case "$arch" in
    arm64|amd64) ;;
    *)
      echo "Error: invalid Linux architecture '$arch' (expected arm64, x86_64, amd64, or both)"
      exit 1
      ;;
  esac
done

if ! command -v docker >/dev/null 2>&1; then
  echo "Error: docker is not installed or not in PATH."
  exit 1
fi

case "$step" in
  build) container_command="./scripts/linux/common/build.sh" ;;
  package) container_command="./scripts/linux/common/package.sh" ;;
  all) container_command="./scripts/linux/common/build.sh && ./scripts/linux/common/package.sh" ;;
esac

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
  echo "Running $arch $step step in Docker..."
  docker run --rm \
    --platform "$platform" \
    -e "CAVEVIEWER_LINUX_DOCKER_BUILD=1" \
    -e "CAVEVIEWER_LINUX_BUILD_VENV=$linux_build_venv_in_container" \
    -v "$repo_root:/workspace" \
    -w /workspace \
    "$image_name" \
    bash -c "$container_command"
done

echo ""
if [ "$step" = "build" ]; then
  echo "Done. Build directories:"
  find "$repo_root/dist/linux" -path "*/app/CaveViewer" -type d -print 2>/dev/null | sort || echo "  (dist/linux/*/app/ not found -- check build output above)"
else
  echo "Done. Artifacts:"
  find "$repo_root/dist/linux" -path "*/packages/*.AppImage" -print 2>/dev/null | sort || echo "  (dist/linux/*/packages/ not found -- check build output above)"
fi
