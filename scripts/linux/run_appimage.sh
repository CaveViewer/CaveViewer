#!/bin/bash
# Run AppImage GUI in Docker with X11 forwarding (macOS/Linux)

APPIMAGE="$(cd "$(dirname "$0")/../.." && pwd)/dist/linux/packages/CaveViewer-1.2.45-x86_64.AppImage"

if [ ! -f "$APPIMAGE" ]; then
  echo "Error: AppImage not found at $APPIMAGE"
  exit 1
fi

echo "Launching CaveViewer in Docker..."
echo ""

# For macOS with Docker Desktop, use host.docker.internal
if [[ "$OSTYPE" == "darwin"* ]]; then
  echo "Note: GUI display forwarding not available on macOS Docker Desktop"
  echo "Use option 2 to extract and run on native Linux instead"
  exit 1
fi

# For Linux with X11
if [ -z "$DISPLAY" ]; then
  echo "Error: DISPLAY not set. Run on a Linux system with X11 or use WSL2"
  exit 1
fi

docker run --rm \
  -v "$APPIMAGE:/app/CaveViewer.AppImage" \
  -e DISPLAY="$DISPLAY" \
  -v /tmp/.X11-unix:/tmp/.X11-unix \
  -v "$HOME/.Xauthority:$HOME/.Xauthority" \
  ubuntu:24.04 bash << 'EOF'
apt-get update -qq && apt-get install -y -qq \
  libfreetype6 libglx0 libgl1-mesa-dri ca-certificates \
  libxkbcommon0 libxkbcommon-x11-0 > /dev/null

cd /tmp
chmod +x /app/CaveViewer.AppImage
/app/CaveViewer.AppImage
EOF
