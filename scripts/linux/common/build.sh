#!/usr/bin/env bash
set -euo pipefail

# Linux app bundle builder.
# Internal Docker-only entry point that builds a standalone PyInstaller app
# bundle under dist/linux/<arch>/app.
#
# Do not run this script directly. Use release.sh or build_linux_in_docker.sh.

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "$script_dir/../../.." && pwd)"

print_usage() {
  cat <<'EOF'
Usage:
  build.sh --help

Internal Docker-only script. Use one of:
  release.sh --target=linux-arm64 --version=<version> --notes "Release notes" --action=build
  release.sh --target=linux-x86_64 --version=<version> --notes "Release notes" --action=build
EOF
}

if [ "${1:-}" = "-h" ] || [ "${1:-}" = "--help" ]; then
  print_usage
  exit 0
fi

if [ "${CAVEVIEWER_LINUX_DOCKER_BUILD:-}" != "1" ]; then
  echo "Error: Linux release builds must run through Docker."
  echo ""
  print_usage
  exit 1
fi

if [ "$#" -gt 0 ]; then
  if [[ "$1" == -* ]]; then
    echo "Error: unknown option '$1'"
  else
    echo "Error: positional arguments are not supported: '$1'"
  fi
  echo ""
  print_usage
  exit 1
fi

linux_arch_tag=""
linux_dist_arch=""
case "$(uname -m)" in
  x86_64)
    linux_arch_tag="amd64"
    linux_dist_arch="x86_64"
    ;;
  aarch64|arm64)
    linux_arch_tag="arm64"
    linux_dist_arch="arm64"
    ;;
esac
if [ -z "$linux_dist_arch" ]; then
  echo "Error: unsupported Linux architecture $(uname -m)"
  exit 1
fi
linux_venv_default="$repo_root/.venv-linux-build"
if [ -n "$linux_arch_tag" ]; then
  linux_venv_default="$repo_root/.venv-linux-build-$linux_arch_tag"
fi
# Keep Linux build dependencies isolated from the main developer venv.
venv_dir="${CAVEVIEWER_LINUX_BUILD_VENV:-$linux_venv_default}"
spec_file="$repo_root/CaveViewer.spec"
dist_app_dir="$repo_root/dist/linux/$linux_dist_arch/app"
work_dir="$repo_root/build/pyinstaller/linux/$linux_dist_arch"

# python-build-standalone: Python binaries compiled against glibc 2.17 so the
# bundled libpython3.12.so won't require GLIBC_2.38 on older distros.
# Keep this on the Python series we support, but discover the exact patch
# release from GitHub so a stale filename does not break CI release builds.
# Override for reproducible/debug builds with:
#   CAVEVIEWER_STANDALONE_PYTHON_SERIES=3.12
#   CAVEVIEWER_STANDALONE_PYTHON_TAG=20260623
PBS_PYTHON_SERIES="${CAVEVIEWER_STANDALONE_PYTHON_SERIES:-3.12}"
PBS_TAG="${CAVEVIEWER_STANDALONE_PYTHON_TAG:-latest}"

resolve_portable_python_asset() {
  local arch="$1"

  python3 - "$arch" "$PBS_PYTHON_SERIES" "$PBS_TAG" <<'PY'
import json
import re
import sys
import urllib.error
import urllib.request

arch, python_series, tag = sys.argv[1:4]
base_url = "https://api.github.com/repos/astral-sh/python-build-standalone/releases"
api_url = f"{base_url}/latest" if tag == "latest" else f"{base_url}/tags/{tag}"

try:
    with urllib.request.urlopen(api_url, timeout=30) as response:
        release = json.load(response)
except urllib.error.HTTPError as exc:
    print(f"Error: python-build-standalone release lookup failed: HTTP {exc.code} {api_url}", file=sys.stderr)
    sys.exit(1)
except Exception as exc:
    print(f"Error: python-build-standalone release lookup failed: {exc}", file=sys.stderr)
    sys.exit(1)

pattern = re.compile(
    rf"^cpython-{re.escape(python_series)}\.\d+\+\d+-"
    rf"{re.escape(arch)}-install_only\.tar\.gz$"
)

matches = []
for asset in release.get("assets", []):
    name = asset.get("name", "")
    url = asset.get("browser_download_url", "")
    if pattern.match(name) and url:
        matches.append((name, url))

if not matches:
    release_name = release.get("tag_name") or tag
    print(
        "Error: no python-build-standalone asset matched "
        f"Python {python_series}, arch {arch}, release {release_name}",
        file=sys.stderr,
    )
    sys.exit(1)

matches.sort()
print(matches[-1][0])
print(matches[-1][1])
PY
}

ensure_standalone_python_toolchain_shims() {
  local shim_dir="/tools/llvm/bin"
  local created_with_sudo=false

  if [ -x "$shim_dir/llvm-ar" ] && [ -x "$shim_dir/llvm-ranlib" ] && [ -x "$shim_dir/llvm-nm" ]; then
    return 0
  fi

  for tool in ar ranlib nm; do
    if ! command -v "$tool" >/dev/null 2>&1; then
      echo "Error: required build tool not found: $tool"
      exit 1
    fi
  done

  if mkdir -p "$shim_dir" 2>/dev/null; then
    :
  elif command -v sudo >/dev/null 2>&1; then
    sudo mkdir -p "$shim_dir"
    created_with_sudo=true
  else
    echo "Warning: could not create $shim_dir; Pillow may fail if standalone Python references LLVM tools there."
    return 0
  fi

  if $created_with_sudo; then
    sudo ln -sf "$(command -v ar)" "$shim_dir/llvm-ar"
    sudo ln -sf "$(command -v ranlib)" "$shim_dir/llvm-ranlib"
    sudo ln -sf "$(command -v nm)" "$shim_dir/llvm-nm"
  else
    ln -sf "$(command -v ar)" "$shim_dir/llvm-ar"
    ln -sf "$(command -v ranlib)" "$shim_dir/llvm-ranlib"
    ln -sf "$(command -v nm)" "$shim_dir/llvm-nm"
  fi

  for llvm_tool in llvm-ar llvm-ranlib llvm-nm; do
    if [ ! -x "$shim_dir/$llvm_tool" ]; then
      echo "Error: failed to create standalone Python toolchain shim: $shim_dir/$llvm_tool"
      exit 1
    fi
  done

  echo "Standalone Python toolchain shims:"
  ls -l "$shim_dir/llvm-ar" "$shim_dir/llvm-ranlib" "$shim_dir/llvm-nm"
}

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

  local asset_info tarball url
  asset_info="$(resolve_portable_python_asset "$arch")"
  tarball="$(printf '%s\n' "$asset_info" | sed -n '1p')"
  url="$(printf '%s\n' "$asset_info" | sed -n '2p')"

  echo "Downloading portable Python ${tarball} (glibc 2.17 compatible)..." >&2
  mkdir -p "$cache_dir"
  if command -v curl >/dev/null 2>&1; then
    curl -fsSL --progress-bar -o "$cache_dir/$tarball" "$url"
  elif command -v wget >/dev/null 2>&1; then
    wget -q --show-progress -O "$cache_dir/$tarball" "$url"
  else
    echo "Error: curl or wget is required to download standalone Python."; exit 1
  fi
  if [ ! -s "$cache_dir/$tarball" ]; then
    echo "Error: downloaded standalone Python tarball is missing or empty: $cache_dir/$tarball"
    exit 1
  fi
  tar -xzf "$cache_dir/$tarball" -C "$cache_dir"
  rm "$cache_dir/$tarball"
  if [ ! -x "$python_bin" ]; then
    echo "Error: standalone Python extraction did not produce: $python_bin"
    exit 1
  fi
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
  "fontconfig"
  "fonts-noto-core"
  "python3-dev"
  "python3-tk"
  "tk-dev"
  "tcl-dev"
  "libfreetype6-dev"
  "libjpeg-dev"
  "zlib1g-dev"
  "libtiff-dev"
  "libwebp-dev"
  "libopenjp2-7-dev"
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
ensure_standalone_python_toolchain_shims

if [ ! -x "$venv_dir/bin/python" ]; then
  echo "Creating virtual environment at $venv_dir..."
  "$standalone_python" -m venv "$venv_dir"
fi
python_exe="$venv_dir/bin/python"
echo "Using venv: $venv_dir"

# python-build-standalone can carry sysconfig paths from its build host
# (for example /tools/llvm/bin/llvm-ar). Those paths do not exist on GitHub
# Actions or ordinary distro build hosts, and Pillow's source build respects
# them unless we point distutils/setuptools back at the local binutils.
export AR="${AR:-ar}"
export RANLIB="${RANLIB:-ranlib}"
export NM="${NM:-nm}"

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
  --add-data "$repo_root/security:security" \
  --add-data "$repo_root/LICENSE:." \
  --add-data "$repo_root/THIRD_PARTY_NOTICES.md:." \
  "$repo_root/caveviewer.py"

app_dir="$dist_app_dir/CaveViewer"
if [ ! -d "$app_dir" ]; then
  echo "Error: build completed but app directory not found at $app_dir"
  exit 1
fi

echo "Build complete: $app_dir"
echo "Note: CaveViewer/ is an intermediate build artifact."
release_target="linux-x86_64"
if [ "$linux_dist_arch" = "arm64" ]; then
  release_target="linux-arm64"
fi
echo "Run release.sh --target=$release_target --version=<version> --notes \"Release notes\" --action=package to generate the distributable AppImage in dist/linux/$linux_dist_arch/packages/."
