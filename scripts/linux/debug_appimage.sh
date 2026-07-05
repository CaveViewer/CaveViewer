#!/bin/bash
# Debug script to run Linux AppImage and capture all errors

APPIMAGE=/Users/vitaliy/Dev/cave-viewer/CaveViewerMac/dist/linux/packages/CaveViewer-1.2.45-x86_64.AppImage

if [ ! -f "$APPIMAGE" ]; then
  echo "Error: AppImage not found at $APPIMAGE"
  exit 1
fi

# Extract to temp directory
EXTRACT_DIR=$(mktemp -d)
trap "rm -rf $EXTRACT_DIR" EXIT

echo "Extracting AppImage to $EXTRACT_DIR..."
cd "$EXTRACT_DIR"
tail -n +22 "$APPIMAGE" | tar xzf - || exit 1

echo ""
echo "=== Directory structure ==="
ls -la CaveViewer/

echo ""
echo "=== Checking for gui/assets ==="
ls -la CaveViewer/gui/assets/ || echo "ERROR: gui/assets not found!"

echo ""
echo "=== Checking for gui/assets/app_mark_transparent.png ==="
if [ -f "CaveViewer/gui/assets/app_mark_transparent.png" ]; then
  echo "✓ app_mark_transparent.png found"
  file CaveViewer/gui/assets/app_mark_transparent.png
else
  echo "✗ app_mark_transparent.png NOT found"
fi

echo ""
echo "=== Attempting to run CaveViewer ==="
echo "Output will show below. Press Ctrl+C to stop."
echo ""

export PYTHONUNBUFFERED=1
cd "$EXTRACT_DIR"
"./CaveViewer/CaveViewer" 2>&1 | head -100
