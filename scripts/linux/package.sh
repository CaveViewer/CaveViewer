#!/usr/bin/env bash
set -euo pipefail

# Package a standalone Linux app bundle as a standard AppDir/AppImage.
#
# Usage:
#   ./scripts/linux/package.sh

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "$script_dir/../.." && pwd)"
dist_app_dir="$repo_root/dist/linux/app"
app_dir="$dist_app_dir/CaveViewer"
appdir="$dist_app_dir/CaveViewer.AppDir"
dist_packages_dir="$repo_root/dist/linux/packages"
icon_src="$repo_root/gui/assets/app_icon_macos.png"

# Extract version info from Python file.
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

if [ ! -f "$icon_src" ]; then
  echo "Error: app icon not found at $icon_src"
  exit 1
fi

find_appimagetool() {
  if command -v appimagetool >/dev/null 2>&1; then
    command -v appimagetool
    return 0
  fi
  if command -v appimagetool-x86_64.AppImage >/dev/null 2>&1; then
    command -v appimagetool-x86_64.AppImage
    return 0
  fi
  if command -v appimagetool-aarch64.AppImage >/dev/null 2>&1; then
    command -v appimagetool-aarch64.AppImage
    return 0
  fi
  return 1
}

appimagetool_matches_arch() {
  local path="$1"
  local expected_arch="$2"
  local file_info
  file_info="$(file "$path" 2>/dev/null || true)"
  case "$expected_arch" in
    x86_64)
      [[ "$file_info" == *"x86-64"* || "$file_info" == *"x86_64"* ]]
      ;;
    aarch64)
      [[ "$file_info" == *"aarch64"* || "$file_info" == *"ARM aarch64"* ]]
      ;;
    *)
      return 1
      ;;
  esac
}

appimagetool_can_execute() {
  local path="$1"
  APPIMAGE_EXTRACT_AND_RUN=1 "$path" --appimage-version >/dev/null 2>&1 ||
    APPIMAGE_EXTRACT_AND_RUN=1 "$path" --version >/dev/null 2>&1 ||
    APPIMAGE_EXTRACT_AND_RUN=1 "$path" --help >/dev/null 2>&1
}

appimagetool_is_usable() {
  local path="$1"
  local expected_arch="$2"
  appimagetool_matches_arch "$path" "$expected_arch" && appimagetool_can_execute "$path"
}

ensure_appimagetool() {
  local expected_arch="$1"
  local candidate
  candidate="$(find_appimagetool || true)"
  if [ -n "$candidate" ] && appimagetool_is_usable "$candidate" "$expected_arch"; then
    echo "$candidate"
    return 0
  fi

  if [ -n "$candidate" ]; then
    echo "Ignoring unusable appimagetool: $candidate" >&2
    file "$candidate" >&2 || true
  fi

  local tools_dir="$repo_root/dist/linux/tools"
  local downloaded="$tools_dir/appimagetool-${expected_arch}.AppImage"
  mkdir -p "$tools_dir"
  if [ ! -x "$downloaded" ] || ! appimagetool_is_usable "$downloaded" "$expected_arch"; then
    echo "Downloading appimagetool for $expected_arch..." >&2
    curl -fsSL -o "$downloaded" \
      "https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-${expected_arch}.AppImage"
    chmod +x "$downloaded"
  fi

  if ! appimagetool_is_usable "$downloaded" "$expected_arch"; then
    echo "Error: downloaded appimagetool cannot run in this build environment:" >&2
    file "$downloaded" >&2 || true
    echo "This usually means Docker/QEMU cannot execute $expected_arch AppImages on this host." >&2
    return 1
  fi

  echo "$downloaded"
}

ARCH="$(uname -m)"
case "$ARCH" in
  x86_64) appimage_arch="x86_64" ;;
  aarch64|arm64) appimage_arch="aarch64" ;;
  *) appimage_arch="$ARCH" ;;
esac

appimagetool_path="$(ensure_appimagetool "$appimage_arch")"
echo "Using appimagetool: $appimagetool_path"

echo "Packaging CaveViewer v$APP_VERSION..."

mkdir -p "$dist_packages_dir"
rm -rf "$appdir"

output_appimage="$dist_packages_dir/CaveViewer-${APP_VERSION}-${ARCH}.AppImage"
rm -f "$output_appimage"

mkdir -p \
  "$appdir/usr/lib/caveviewer" \
  "$appdir/usr/share/applications" \
  "$appdir/usr/share/icons/hicolor"

cp -a "$app_dir/." "$appdir/usr/lib/caveviewer/"
bundled_font_dir="$appdir/usr/share/caveviewer/fonts"
bundled_ui_font="$bundled_font_dir/CaveViewerUI-Regular.ttf"
mkdir -p "$bundled_font_dir"
ui_font_candidates=(
  "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf"
  "/usr/share/fonts/google-noto/NotoSans-Regular.ttf"
  "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
  "/usr/share/fonts/dejavu-sans-fonts/DejaVuSans.ttf"
  "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf"
  "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf"
)
for ui_font_candidate in "${ui_font_candidates[@]}"; do
  if [ -f "$ui_font_candidate" ]; then
    cp "$ui_font_candidate" "$bundled_ui_font"
    echo "Bundled UI font: $ui_font_candidate"
    break
  fi
done
if [ ! -f "$bundled_ui_font" ]; then
  echo "Warning: no preferred UI font was found to bundle; runtime font fallback will be used."
fi

icon_hicolor_dir="$appdir/usr/share/icons/hicolor"
icon_root="$appdir/caveviewer.png"
linux_arch_tag=""
case "$(uname -m)" in
  x86_64) linux_arch_tag="amd64" ;;
  aarch64|arm64) linux_arch_tag="arm64" ;;
esac
linux_venv_default="$repo_root/.venv-linux-build"
if [ -n "$linux_arch_tag" ]; then
  linux_venv_default="$repo_root/.venv-linux-build-$linux_arch_tag"
fi
icon_python="${CAVEVIEWER_LINUX_BUILD_VENV:-$linux_venv_default}/bin/python"
if [ -x "$icon_python" ]; then
  "$icon_python" -c '
import pathlib
import sys
from PIL import Image

src = pathlib.Path(sys.argv[1])
icon_dir = pathlib.Path(sys.argv[2])
root_dest = pathlib.Path(sys.argv[3])
sizes = (48, 64, 128, 256, 512)

img = Image.open(src).convert("RGBA")
for size in sizes:
    resized = img.copy()
    resized.thumbnail((size, size), Image.LANCZOS)
    canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    canvas.alpha_composite(resized, ((size - resized.width) // 2, (size - resized.height) // 2))
    dest = icon_dir / f"{size}x{size}" / "apps" / "caveviewer.png"
    dest.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(dest)

root_256 = icon_dir / "256x256" / "apps" / "caveviewer.png"
root_dest.write_bytes(root_256.read_bytes())
' "$icon_src" "$icon_hicolor_dir" "$icon_root"
else
  mkdir -p "$icon_hicolor_dir/256x256/apps"
  cp "$icon_src" "$icon_hicolor_dir/256x256/apps/caveviewer.png"
  cp "$icon_src" "$icon_root"
fi

cat > "$appdir/caveviewer.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=CaveViewer
Comment=Explore large 3-D cave maps
Exec=AppRun
Icon=caveviewer
Terminal=false
Categories=Graphics;Science;Viewer;
StartupWMClass=CaveViewer
EOF
cp "$appdir/caveviewer.desktop" "$appdir/usr/share/applications/caveviewer.desktop"

cat > "$appdir/AppRun" <<'APP_RUN_EOF'
#!/usr/bin/env bash
set -euo pipefail

debug="${CAVEVIEWER_LAUNCH_DEBUG:-0}"
launcher_version="2026-07-04-debug-stream-v2"
log_file="${CAVEVIEWER_LAUNCH_LOG:-${TMPDIR:-/tmp}/caveviewer-launch.log}"

if [ -n "${APPDIR:-}" ]; then
  appdir="$APPDIR"
elif command -v readlink >/dev/null 2>&1; then
  appdir="$(dirname "$(readlink -f "$0")")"
else
  appdir="$(cd "$(dirname "$0")" && pwd)"
fi

runtime_root="${XDG_RUNTIME_DIR:-${TMPDIR:-/tmp}}"
if [ ! -d "$runtime_root" ] || [ ! -w "$runtime_root" ]; then
  runtime_root="/tmp"
fi
gl_compat_dir="$(mktemp -d "$runtime_root/caveviewer-gl-compat.XXXXXX")"
cleanup() {
  rm -rf "$gl_compat_dir"
}
trap cleanup EXIT

if [ "$debug" = "1" ]; then
  echo "[CaveViewer AppRun] launcher_version=$launcher_version"
  echo "[CaveViewer AppRun] launch_log=$log_file"
  echo "[CaveViewer AppRun] APPDIR=$appdir"
  echo "[CaveViewer AppRun] runtime_root=$runtime_root"
  echo "[CaveViewer AppRun] gl_compat_dir=$gl_compat_dir"
  echo "[CaveViewer AppRun] uname=$(uname -a)"
fi

install_desktop_integration() {
  if [ "${CAVEVIEWER_NO_DESKTOP_INTEGRATION:-0}" = "1" ]; then
    return
  fi
  if [ -z "${APPIMAGE:-}" ] || [ -z "${HOME:-}" ]; then
    return
  fi

  data_home="${XDG_DATA_HOME:-$HOME/.local/share}"
  applications_dir="$data_home/applications"
  icons_hicolor_dir="$data_home/icons/hicolor"
  desktop_path="$applications_dir/caveviewer.desktop"

  mkdir -p "$applications_dir"
  if [ -d "$appdir/usr/share/icons/hicolor" ]; then
    mkdir -p "$icons_hicolor_dir"
    cp -R "$appdir/usr/share/icons/hicolor/." "$icons_hicolor_dir/"
  fi

  cat > "$desktop_path" <<DESKTOP_EOF
[Desktop Entry]
Type=Application
Name=CaveViewer
Comment=Explore large 3-D cave maps
Exec="$APPIMAGE"
Icon=caveviewer
Terminal=false
Categories=Graphics;Science;Viewer;
StartupWMClass=CaveViewer
DESKTOP_EOF
  chmod 0644 "$desktop_path" 2>/dev/null || true
  find "$icons_hicolor_dir" -path "*/apps/caveviewer.png" -exec chmod 0644 {} \; 2>/dev/null || true
  if command -v gtk-update-icon-cache >/dev/null 2>&1; then
    gtk-update-icon-cache -q -t "$icons_hicolor_dir" >/dev/null 2>&1 || true
  fi
  if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database -q "$applications_dir" >/dev/null 2>&1 || true
  fi

  if [ "$debug" = "1" ]; then
    echo "[CaveViewer AppRun] Desktop file: $desktop_path"
    echo "[CaveViewer AppRun] Desktop icons: $icons_hicolor_dir/*/apps/caveviewer.png"
  fi
}

install_desktop_integration

# On RPM-based distros (Fedora, RHEL, etc.) the unversioned libGL.so /
# libEGL.so symlinks are often only available in -devel packages. Create
# temporary compat symlinks so PyInstaller's ctypes hook can resolve them.
if [ "$debug" = "1" ]; then
  echo "[CaveViewer AppRun] Checking OpenGL compatibility libraries..."
fi
if command -v ldconfig >/dev/null 2>&1; then
  for lib_base in libGL libEGL libGLU libGLES1_CM libGLESv2; do
    unversioned="${lib_base}.so"
    if ! ldconfig -p 2>/dev/null | grep -q "[[:space:]]${unversioned}[[:space:]]"; then
      versioned_path=$(ldconfig -p 2>/dev/null | grep "[[:space:]]${lib_base}\.so\." | awk '{print $NF}' | sort -V | tail -1 || true)
      if [ -n "$versioned_path" ]; then
        ln -sf "$versioned_path" "$gl_compat_dir/${unversioned}"
        if [ "$debug" = "1" ]; then
          echo "[CaveViewer AppRun] Linked $unversioned -> $versioned_path"
        fi
      elif [ "$debug" = "1" ]; then
        echo "[CaveViewer AppRun] No versioned candidate found for $unversioned"
      fi
    fi
  done
fi
if [ "$debug" = "1" ]; then
  echo "[CaveViewer AppRun] OpenGL compatibility check complete."
fi

bundled_internal_dir="$appdir/usr/lib/caveviewer/_internal"
bundled_tcl_dir="$bundled_internal_dir/_tcl_data"
bundled_tk_dir="$bundled_internal_dir/_tk_data"
bundled_ui_font="$appdir/usr/share/caveviewer/fonts/CaveViewerUI-Regular.ttf"

# Prefer the distro Tcl/Tk/font stack, matching the 1.0.38 Linux builds whose
# splash text rendered correctly. The PyInstaller-bundled Tcl/Tk libraries are
# kept available behind CAVEVIEWER_USE_BUNDLED_TK=1 for systems that truly need
# them, but forcing them by default can lose Tk's normal Xft/fontconfig path and
# make text look like old bitmap fonts.
if [ "${CAVEVIEWER_USE_BUNDLED_TK:-0}" = "1" ]; then
  export LD_LIBRARY_PATH="$gl_compat_dir:$bundled_internal_dir:$appdir/usr/lib/caveviewer/lib:$appdir/usr/lib/caveviewer/lib64:${LD_LIBRARY_PATH:-}"
else
  export LD_LIBRARY_PATH="$gl_compat_dir:$appdir/usr/lib/caveviewer/lib:$appdir/usr/lib/caveviewer/lib64:${LD_LIBRARY_PATH:-}"
fi
if [ -f "$bundled_ui_font" ]; then
  export CAVEVIEWER_UI_FONT="${CAVEVIEWER_UI_FONT:-$bundled_ui_font}"
fi
export CAVEVIEWER_TEXT_AA_MODE="${CAVEVIEWER_TEXT_AA_MODE:-light}"
if [ "${CAVEVIEWER_USE_BUNDLED_TK:-0}" = "1" ] && [ -d "$bundled_tcl_dir" ]; then
  export TCL_LIBRARY="${TCL_LIBRARY:-$bundled_tcl_dir}"
else
  export TCL_LIBRARY="${TCL_LIBRARY:-/usr/share/tcltk/tcl8.6}"
fi
if [ "${CAVEVIEWER_USE_BUNDLED_TK:-0}" = "1" ] && [ -d "$bundled_tk_dir" ]; then
  export TK_LIBRARY="${TK_LIBRARY:-$bundled_tk_dir}"
else
  export TK_LIBRARY="${TK_LIBRARY:-/usr/share/tcltk/tk8.6}"
fi

executable="$appdir/usr/lib/caveviewer/CaveViewer"
if [ ! -x "$executable" ]; then
  echo "Error: CaveViewer executable not found or not executable:"
  echo "  $executable"
  exit 1
fi

{
  echo "[CaveViewer AppRun] $(date -u '+%Y-%m-%dT%H:%M:%SZ')"
  echo "[CaveViewer AppRun] launcher_version=$launcher_version"
  echo "[CaveViewer AppRun] APPDIR=$appdir"
  echo "[CaveViewer AppRun] executable=$executable"
  echo "[CaveViewer AppRun] LD_LIBRARY_PATH=$LD_LIBRARY_PATH"
  echo "[CaveViewer AppRun] CAVEVIEWER_USE_BUNDLED_TK=${CAVEVIEWER_USE_BUNDLED_TK:-0}"
  echo "[CaveViewer AppRun] CAVEVIEWER_UI_FONT=${CAVEVIEWER_UI_FONT:-}"
  echo "[CaveViewer AppRun] CAVEVIEWER_TEXT_AA_MODE=${CAVEVIEWER_TEXT_AA_MODE:-}"
  echo "[CaveViewer AppRun] FONTCONFIG_FILE=${FONTCONFIG_FILE:-}"
  echo "[CaveViewer AppRun] TCL_LIBRARY=$TCL_LIBRARY"
  echo "[CaveViewer AppRun] TK_LIBRARY=$TK_LIBRARY"
  echo "[CaveViewer AppRun] args=$*"
} > "$log_file" 2>&1

if [ "$debug" = "1" ]; then
  echo "[CaveViewer AppRun] executable=$executable"
  echo "[CaveViewer AppRun] LD_LIBRARY_PATH=$LD_LIBRARY_PATH"
  echo "[CaveViewer AppRun] CAVEVIEWER_USE_BUNDLED_TK=${CAVEVIEWER_USE_BUNDLED_TK:-0}"
  echo "[CaveViewer AppRun] CAVEVIEWER_UI_FONT=${CAVEVIEWER_UI_FONT:-}"
  echo "[CaveViewer AppRun] CAVEVIEWER_TEXT_AA_MODE=${CAVEVIEWER_TEXT_AA_MODE:-}"
  echo "[CaveViewer AppRun] FONTCONFIG_FILE=${FONTCONFIG_FILE:-}"
  echo "[CaveViewer AppRun] TCL_LIBRARY=$TCL_LIBRARY"
  echo "[CaveViewer AppRun] TK_LIBRARY=$TK_LIBRARY"
  echo "[CaveViewer AppRun] Starting CaveViewer..."
fi

set +e
if [ "$debug" = "1" ]; then
  "$executable" "$@" 2>&1 | tee -a "$log_file"
  exit_code=${PIPESTATUS[0]}
else
  "$executable" "$@" >> "$log_file" 2>&1
  exit_code=$?
fi

if [ "$exit_code" -ne 0 ] && [ "${CAVEVIEWER_USE_BUNDLED_TK:-0}" != "1" ] &&
   { [ -d "$bundled_tcl_dir" ] || [ -d "$bundled_tk_dir" ]; } &&
   grep -Eq "lib(tcl|tk)[0-9.]*\.so.*cannot open shared object file" "$log_file"; then
  echo "[CaveViewer AppRun] System Tcl/Tk library missing; retrying with bundled Tcl/Tk." | tee -a "$log_file"
  export CAVEVIEWER_USE_BUNDLED_TK=1
  export LD_LIBRARY_PATH="$gl_compat_dir:$bundled_internal_dir:$appdir/usr/lib/caveviewer/lib:$appdir/usr/lib/caveviewer/lib64:${LD_LIBRARY_PATH:-}"
  if [ -d "$bundled_tcl_dir" ]; then
    export TCL_LIBRARY="$bundled_tcl_dir"
  fi
  if [ -d "$bundled_tk_dir" ]; then
    export TK_LIBRARY="$bundled_tk_dir"
  fi
  if [ "$debug" = "1" ]; then
    echo "[CaveViewer AppRun] Retry LD_LIBRARY_PATH=$LD_LIBRARY_PATH"
    echo "[CaveViewer AppRun] Retry TCL_LIBRARY=$TCL_LIBRARY"
    echo "[CaveViewer AppRun] Retry TK_LIBRARY=$TK_LIBRARY"
    "$executable" "$@" 2>&1 | tee -a "$log_file"
    exit_code=${PIPESTATUS[0]}
  else
    "$executable" "$@" >> "$log_file" 2>&1
    exit_code=$?
  fi
fi
set -e

if [ "$debug" = "1" ]; then
  echo "[CaveViewer AppRun] CaveViewer exited with code $exit_code"
  echo "[CaveViewer AppRun] Launch log: $log_file"
fi
if [ "$exit_code" -ne 0 ]; then
  echo "CaveViewer exited with code $exit_code."
  echo "Launch log: $log_file"
  echo ""
  tail -n 80 "$log_file" || true
fi
exit "$exit_code"
APP_RUN_EOF
chmod +x "$appdir/AppRun"
echo "AppRun launcher marker:"
grep "launcher_version=" "$appdir/AppRun"

# AppImageKit expects these at the AppDir root.
ln -sfn "usr/lib/caveviewer/CaveViewer" "$appdir/CaveViewer"

echo "Creating AppImage..."
if ! ARCH="$appimage_arch" APPIMAGE_EXTRACT_AND_RUN=1 "$appimagetool_path" "$appdir" "$output_appimage"; then
  echo ""
  echo "Error: appimagetool failed before creating the AppImage."
  echo "AppDir was left in place for inspection: $appdir"
  echo "Expected output was: $output_appimage"
  exit 1
fi
if [ ! -f "$output_appimage" ]; then
  echo ""
  echo "Error: appimagetool completed but did not create the expected AppImage:"
  echo "  $output_appimage"
  echo "AppDir was left in place for inspection: $appdir"
  exit 1
fi
chmod +x "$output_appimage"

echo ""
echo "====================================================="
echo "Package created successfully!"
echo "====================================================="
echo "AppDir: $appdir"
echo "Output: $output_appimage"
echo "Size: $(du -h "$output_appimage" | cut -f1)"
echo ""
echo "To run:"
echo "  $output_appimage"
