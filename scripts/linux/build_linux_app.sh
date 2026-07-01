#!/usr/bin/env bash
set -euo pipefail

# Build a standalone Linux application bundle from source using PyInstaller.
# This is an intermediate artifact for AppImage packaging.
#
# Usage:
#   ./scripts/linux/build_linux_app.sh

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "$script_dir/../.." && pwd)"
venv_dir="$repo_root/.venv"
spec_file="$repo_root/CaveViewer.spec"
dist_app_dir="$repo_root/dist/linux/app"
work_dir="$repo_root/build/pyinstaller"

if [[ "$(uname -s)" != "Linux" ]]; then
  echo "Error: this script must be run on Linux."
  exit 1
fi

# Ensure build dependencies are installed
echo "Checking build dependencies..."
missing_packages=()

# Check for required packages
required_packages=(
  "build-essential"
  "python3-dev"
  "python3-tk"
  "tk-dev"
  "tcl-dev"
  "libfreetype6-dev"
)

for pkg in "${required_packages[@]}"; do
  if ! dpkg -l | grep -q "^ii.*$pkg"; then
    missing_packages+=("$pkg")
  fi
done

if [ ${#missing_packages[@]} -gt 0 ]; then
  echo "Installing missing build dependencies: ${missing_packages[*]}"
  sudo apt-get update
  sudo apt-get install -y "${missing_packages[@]}"
fi

echo "✓ All build dependencies installed"
echo ""
# Detect Docker by checking if /build directory exists (set in Dockerfile)
if [ -d "/build" ] && [ "$(id -u)" == "0" ]; then
  # Running in Docker as root - use system Python
  python_exe="python3"
  echo "Running in Docker container - using system Python"
else
  # Running locally - use venv for isolation
  if [ ! -x "$venv_dir/bin/python" ]; then
    echo "Creating virtual environment at $venv_dir..."
    python3 -m venv "$venv_dir"
  fi
  python_exe="$venv_dir/bin/python"
  echo "Using venv: $venv_dir"
fi

"$python_exe" -m pip install --upgrade pip setuptools

# Install dependencies with --no-binary for Pillow so it compiles from source with Tkinter support
"$python_exe" -m pip install --upgrade --no-binary :all: -r "$repo_root/requirements.txt"

# Verify Pillow was compiled with Tkinter support
echo "Verifying Pillow installation..."
"$python_exe" -c "from PIL import Image; import PIL._tkinter_finder; print('✓ Pillow compiled with Tkinter support')" || {
  echo "ERROR: Pillow was not compiled with Tkinter support"
  echo "Attempting to rebuild Pillow..."
  "$python_exe" -m pip uninstall -y Pillow
  "$python_exe" -m pip install --no-cache-dir --no-binary :all: "Pillow>=10.0.0"
  "$python_exe" -c "from PIL import Image; import PIL._tkinter_finder; print('✓ Pillow compiled with Tkinter support')"
}

"$python_exe" -m pip install --upgrade "pyinstaller==6.21.0"

cd "$repo_root"
mkdir -p "$dist_app_dir" "$work_dir"

# Run PyInstaller with --onedir for AppImage bundling
# Don't use the macOS spec file; generate a Linux-specific onedir build
CAVEVIEWER_APP_ICON="" \
"$python_exe" -m PyInstaller --clean --noconfirm \
  --distpath "$dist_app_dir" \
  --workpath "$work_dir" \
  --onedir \
  --name CaveViewer \
  --hidden-import=PIL._tkinter_finder \
  --hidden-import=tkinter \
  --hidden-import=moderngl_window.context.pyglet \
  --add-data "$repo_root/shaders:shaders" \
  --add-data "$repo_root/gui/assets:gui/assets" \
  "$repo_root/caveviewer.py"

app_dir="$dist_app_dir/CaveViewer"
if [ ! -d "$app_dir" ]; then
  echo "Error: build completed but app directory not found at $app_dir"
  exit 1
fi

echo "Build complete: $app_dir"
echo "Note: CaveViewer/ is an intermediate build artifact."
echo "Run ./scripts/linux/package.sh to generate the distributable AppImage in dist/linux/packages/."
