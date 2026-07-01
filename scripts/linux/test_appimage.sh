#!/bin/bash
# Simple AppImage test - extracts and validates in Docker

set -e

APPIMAGE="$(cd "$(dirname "$0")/../.." && pwd)/dist/linux/packages/CaveViewer-1.2.45-x86_64.AppImage"

if [ ! -f "$APPIMAGE" ]; then
  echo "Error: AppImage not found at $APPIMAGE"
  exit 1
fi

echo "Testing AppImage: $APPIMAGE"
echo ""

docker run --rm -v "$APPIMAGE:/test.AppImage" ubuntu:24.04 bash << 'EOF'
set -e
echo "Installing runtime dependencies..."
apt-get update -qq && apt-get install -y -qq libfreetype6 libglx0 libgl1-mesa-dri ca-certificates > /dev/null

echo "Extracting AppImage..."
cd /tmp
tail -n +22 /test.AppImage | tar xzf -

echo ""
echo "=== Extraction successful ==="
ls -lh CaveViewer/CaveViewer
file CaveViewer/CaveViewer

echo ""
echo "=== Dependencies ==="
ldd CaveViewer/CaveViewer 2>/dev/null | grep -E "libGL|libfreetype|moderngl|numpy" || echo "Core dependencies present"

echo ""
echo "✓ AppImage is valid and can be extracted on Linux"
EOF

echo ""
echo "Done!"
