#!/usr/bin/env bash
if [ "${BASH##*/}" != "bash" ]; then
  echo "Error: build_linux_in_docker.sh must be run with bash, not sh."
  echo "Use: build_linux_in_docker.sh ..."
  exit 1
fi

set -euo pipefail

# Host-side Linux Docker build driver.
# Builds and/or packages Linux release artifacts inside the Docker build
# container. This is the supported host entry point for Linux release artifacts;
# scripts/linux/common/*.sh are container-only internals.
#
# Usage:
#   build_linux_in_docker.sh [--arch=<arm64|x86_64|both>] [--step=<build|package|all>] [--rebuild]
#
# Default builds BOTH architectures. arm64 runs without emulation on Apple
# Silicon; x86_64 runs under QEMU (slower). Each arch gets its own Docker
# image tag so switching between them doesn't force a full image rebuild.

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "$script_dir/../.." && pwd)"
dockerfile="$script_dir/Dockerfile.linux-build"
linux_build_venv_template="${CAVEVIEWER_LINUX_BUILD_VENV:-$repo_root/.venv-linux-build-{arch}}"

print_help() {
  cat <<'EOF'
Usage:
  build_linux_in_docker.sh [--arch=<arm64|x86_64|both>] [--step=<build|package|all>] [--rebuild]

Options:
  --arch=<arch>    Linux architecture to build. Default: both
  --step=<step>    Build step to run. Default: all
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

set_arch() {
  case "$1" in
    both) archs=("arm64" "amd64") ;;
    amd64|x86_64) archs=("amd64") ;;
    *) archs=("$1") ;;
  esac
}

set_step() {
  case "$1" in
    build|package|all) step="$1" ;;
    *)
      echo "Error: invalid --step '$1' (expected build, package, or all)"
      exit 1
      ;;
  esac
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    -h|--help)
      print_help
      exit 0
      ;;
    --rebuild)
      rebuild=true
      shift
      ;;
    --arch=*)
      set_arch "${1#--arch=}"
      shift
      ;;
    --arch)
      shift
      if [ "$#" -eq 0 ]; then
        echo "Error: --arch requires a value."
        exit 1
      fi
      set_arch "$1"
      shift
      ;;
    --step=*)
      set_step "${1#--step=}"
      shift
      ;;
    --step)
      shift
      if [ "$#" -eq 0 ]; then
        echo "Error: --step requires a value."
        exit 1
      fi
      set_step "$1"
      shift
      ;;
    -*)
      echo "Error: unknown option '$1'"
      exit 1
      ;;
    *)
      echo "Error: positional arguments are not supported: '$1'"
      print_help
      exit 1
      ;;
  esac
done

for arch in "${archs[@]}"; do
  case "$arch" in
    arm64|amd64) ;;
    *)
      echo "Error: invalid Linux architecture '$arch' (expected arm64, x86_64, or both)"
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
