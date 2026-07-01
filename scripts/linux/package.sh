#!/usr/bin/env bash
set -euo pipefail

# Package a standalone Linux app bundle as a distributable tarball.
# Supports extraction and direct execution.
#
# Usage:
#   ./scripts/linux/package.sh

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "$script_dir/../.." && pwd)"
dist_app_dir="$repo_root/dist/linux/app"
app_dir="$dist_app_dir/CaveViewer"
dist_packages_dir="$repo_root/dist/linux/packages"

# Extract version info from Python file
APP_NAME=$(grep "^APP_NAME = " "$repo_root/caveviewer_version.py" | grep -oP '"\K[^"]+')
APP_VERSION=$(grep "^APP_VERSION = " "$repo_root/caveviewer_version.py" | grep -oP '"\K[^"]+')

if [[ -z "$APP_NAME" || -z "$APP_VERSION" ]]; then
  echo "Error: Could not extract APP_NAME or APP_VERSION from caveviewer_version.py"
  exit 1
fi

if [[ "$(uname -s)" != "Linux" ]]; then
  echo "Error: this script must be run on Linux."
  exit 1
fi

if [ ! -d "$app_dir" ]; then
  echo "Error: app directory not found at $app_dir"
  echo "Run ./scripts/linux/build_linux_app.sh first."
  exit 1
fi

echo "Packaging CaveViewer v$APP_VERSION..."

mkdir -p "$dist_packages_dir"

# Create distributable tarball with version info
output_tarball="$dist_packages_dir/CaveViewer-${APP_VERSION}-x86_64.tar.gz"
output_appimage="$dist_packages_dir/CaveViewer-${APP_VERSION}-x86_64.AppImage"

# Create tarball of the app
cd "$dist_app_dir"
tar -czf "$output_tarball" CaveViewer/

if [ ! -f "$output_tarball" ]; then
  echo "Error: tarball creation failed"
  exit 1
fi

# Create a simple self-extracting AppImage-like wrapper using tar
# This creates a shell script that extracts and runs the app
cat > "$output_appimage" << 'APPIMAGE_WRAPPER_EOF'
#!/bin/bash
# CaveViewer AppImage-like wrapper script
# Extracts and runs the bundled application

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TEMP_DIR="${TEMP_DIR:-/tmp}"
EXTRACT_DIR="$TEMP_DIR/CaveViewer-runtime-$$"

# Extract the bundled app
mkdir -p "$EXTRACT_DIR"

# Extract everything after the __ARCHIVE_BELOW__ marker
sed -n '/^__ARCHIVE_BELOW__$/,$p' "$0" | tail -n +2 | tar -xzf - -C "$EXTRACT_DIR"

if [ ! -d "$EXTRACT_DIR/CaveViewer" ]; then
  echo "Error: failed to extract AppImage"
  rm -rf "$EXTRACT_DIR"
  exit 1
fi

# Debug: show what was extracted
# echo "DEBUG: AppImage contents:"
# ls -la "$EXTRACT_DIR/CaveViewer/" | head -20

# Set up library paths
export LD_LIBRARY_PATH="$EXTRACT_DIR/CaveViewer/lib:$EXTRACT_DIR/CaveViewer/lib64:${LD_LIBRARY_PATH:-}"

# Make sure we can find system tcl/tk libraries if needed
export TCL_LIBRARY="/usr/share/tcltk/tcl8.6"
export TK_LIBRARY="/usr/share/tcltk/tk8.6"

# Let PyInstaller's bundled Python handle its own module paths
# (do not override PYTHONPATH - it interferes with PyInstaller's _internal discovery)

"$EXTRACT_DIR/CaveViewer/CaveViewer" "$@"
exit_code=$?

# Cleanup
rm -rf "$EXTRACT_DIR"
exit $exit_code
APPIMAGE_WRAPPER_EOF

# Add archive marker before the tarball
echo "__ARCHIVE_BELOW__" >> "$output_appimage"

# Append the tarball to the wrapper script
cat "$output_tarball" >> "$output_appimage"

chmod +x "$output_appimage"

# Clean up tarball (it's now embedded in the AppImage)
rm -f "$output_tarball"

echo ""
echo "====================================================="
echo "Package created successfully!"
echo "====================================================="
echo "Output: $output_appimage"
echo "Size: $(du -h "$output_appimage" | cut -f1)"
echo ""
echo "To run:"
echo "  $output_appimage"
echo ""
echo "To extract:"
echo "  mkdir -p extract_dir"
echo "  cd extract_dir"
echo "  tail -n +9 $output_appimage | tar xzf -"
echo "  ./CaveViewer/CaveViewer"
