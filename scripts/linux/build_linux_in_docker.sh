#!/usr/bin/env bash
set -euo pipefail

# Build CaveViewer Linux AppImage using Docker (works on macOS, Windows, or Linux)
# 
# Usage:
#   ./scripts/linux/build_linux_in_docker.sh              # Build and extract
#   ./scripts/linux/build_linux_in_docker.sh --test       # Build, test, then extract
#   ./scripts/linux/build_linux_in_docker.sh --no-cache   # Force rebuild

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "$script_dir/../.." && pwd)"
dockerfile="$script_dir/Dockerfile"
docker_image="caveviewer-linux-builder"
output_dir="$repo_root/dist/linux/packages"

# Parse arguments
test_mode=false
docker_build_args=""

for arg in "$@"; do
  case "$arg" in
    --test) test_mode=true ;;
    --no-cache) docker_build_args="--no-cache" ;;
    *) echo "Unknown option: $arg"; exit 1 ;;
  esac
done

# Check Docker is available
if ! command -v docker >/dev/null 2>&1; then
  echo "Error: Docker is not installed or not in PATH"
  echo "Install Docker from: https://www.docker.com/products/docker-desktop"
  exit 1
fi

echo "====================================================="
echo "CaveViewer Linux AppImage Builder (Docker)"
echo "====================================================="
echo ""

# Step 1: Build Docker image
echo "[1/3] Building Docker image..."
docker build $docker_build_args \
  -f "$dockerfile" \
  -t "$docker_image" \
  "$repo_root"
echo ""

# Step 2: Extract AppImage (or test first)
if [ "$test_mode" = true ]; then
  echo "[2/3] Testing AppImage in Docker..."
  docker run --rm "$docker_image"
  echo ""
fi

echo "[3/3] Extracting AppImage..."
mkdir -p "$output_dir"

# Run container and copy output
docker run --rm \
  -v "$output_dir":/output \
  "$docker_image" \
  bash -c "cp /app/*.AppImage /output/ 2>/dev/null && echo 'AppImage extracted'"

if [ -z "$(ls -1 "$output_dir"/*.AppImage 2>/dev/null)" ]; then
  echo "Error: No AppImage found in output directory"
  exit 1
fi

echo ""
echo "====================================================="
echo "Build complete!"
echo "====================================================="
appimage_file=$(ls -1 "$output_dir"/*.AppImage | head -1)
appimage_size=$(du -h "$appimage_file" | cut -f1)
appimage_name=$(basename "$appimage_file")

echo "AppImage: $appimage_name"
echo "Size: $appimage_size"
echo "Location: $output_dir"
echo ""
echo "Next steps:"
echo "1. Upload to GitHub releases"
echo "2. Update updates/linux/stable.json with:"
echo "   - SHA256: $(sha256sum "$appimage_file" | cut -d' ' -f1)"
echo "   - Size: $(stat -f%z "$appimage_file" 2>/dev/null || stat -c%s "$appimage_file") bytes"
echo ""
echo "Or test locally:"
echo "  chmod +x $appimage_file"
echo "  ./$appimage_file"
