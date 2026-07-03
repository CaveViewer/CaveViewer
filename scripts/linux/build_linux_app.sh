#!/usr/bin/env bash
set -euo pipefail

# Build a standalone Linux application bundle from source using PyInstaller.
# This is an intermediate artifact for AppImage packaging.
#
# Usage:
#   ./scripts/linux/build_linux_app.sh

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "$script_dir/../.." && pwd)"
linux_arch_tag=""
case "$(uname -m)" in
  x86_64) linux_arch_tag="amd64" ;;
  aarch64|arm64) linux_arch_tag="arm64" ;;
esac
linux_venv_default="$repo_root/.venv-linux-build"
if [ -n "$linux_arch_tag" ]; then
  linux_venv_default="$repo_root/.venv-linux-build-$linux_arch_tag"
fi
# Keep Linux build dependencies isolated from the main developer venv.
venv_dir="${CAVEVIEWER_LINUX_BUILD_VENV:-$linux_venv_default}"
spec_file="$repo_root/CaveViewer.spec"
dist_app_dir="$repo_root/dist/linux/app"
work_dir="$repo_root/build/pyinstaller"

# python-build-standalone: Python binaries compiled against glibc 2.17 so the
# bundled libpython3.12.so won't require GLIBC_2.38 on older distros.
# Update these when upgrading Python: https://github.com/astral-sh/python-build-standalone/releases
PBS_PYTHON_VERSION="3.12.10"
PBS_TAG="20250612"

# Download (or reuse cached) a portable Python binary.
# Prints the path to the python3 executable.
setup_portable_python() {
  local arch
  case "$(uname -m)" in
    x86_64)  arch="x86_64-unknown-linux-gnu" ;;
    aarch64) arch="aarch64-unknown-linux-gnu" ;;
    *) echo "Error: unsupported architecture $(uname -m)"; exit 1 ;;
  esac

  local cache_dir="$repo_root/.cache/standalone-python"
  local python_bin="$cache_dir/python/bin/python3"

  if [ -x "$python_bin" ]; then
    echo "Standalone Python already cached." >&2
    echo "$python_bin"
    return 0
  fi

  local tarball="cpython-${PBS_PYTHON_VERSION}+${PBS_TAG}-${arch}-install_only.tar.gz"
  local url="https://github.com/astral-sh/python-build-standalone/releases/download/${PBS_TAG}/${tarball}"

  echo "Downloading portable Python ${PBS_PYTHON_VERSION} (glibc 2.17 compatible)..." >&2
  mkdir -p "$cache_dir"
  if command -v curl >/dev/null 2>&1; then
    curl -fsSL --progress-bar -o "$cache_dir/$tarball" "$url"
  elif command -v wget >/dev/null 2>&1; then
    wget -q --show-progress -O "$cache_dir/$tarball" "$url"
  else
    echo "Error: curl or wget is required to download standalone Python."; exit 1
  fi
  tar -xzf "$cache_dir/$tarball" -C "$cache_dir"
  rm "$cache_dir/$tarball"
  echo "$python_bin"
}

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
standalone_python=$(setup_portable_python)
echo "Using portable Python: $standalone_python"
echo ""

if [ ! -x "$venv_dir/bin/python" ]; then
  echo "Creating virtual environment at $venv_dir..."
  "$standalone_python" -m venv "$venv_dir"
fi
python_exe="$venv_dir/bin/python"
echo "Using venv: $venv_dir"

"$python_exe" -m pip install --upgrade pip setuptools

# Install dependencies: Pillow must compile from source to pick up Tkinter support.
# All other packages use pre-built manylinux wheels (glibc 2.17 compatible).
"$python_exe" -m pip install --upgrade --no-binary Pillow -r "$repo_root/requirements.txt"

# Verify Pillow was compiled with Tkinter support
echo "Verifying Pillow installation..."
"$python_exe" -c "from PIL import Image; import PIL._tkinter_finder; print('✓ Pillow compiled with Tkinter support')" || {
  echo "ERROR: Pillow was not compiled with Tkinter support"
  echo "Attempting to rebuild Pillow..."
  "$python_exe" -m pip uninstall -y Pillow
  "$python_exe" -m pip install --no-cache-dir --no-binary Pillow "Pillow>=10.0.0"
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
  --specpath "$work_dir" \
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
