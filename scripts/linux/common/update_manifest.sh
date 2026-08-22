#!/usr/bin/env bash
set -euo pipefail

# Linux update manifest writer.
# Writes updates/linux/<arch>/<channel>.json with version, download URL,
# package size, SHA-256, and release notes for the in-app update client.
#
# Usage:
#   update_manifest.sh --version=<version> --download-url=<appimage_url> --artifact-file=<appimage_file> [--notes=<release_notes>] [--channel=<stable|preview>]
# Example:
#   update_manifest.sh --version=1.0.1 \
#     --download-url="https://github.com/<owner>/CaveViewerPlus/releases/download/v1.0.1/CaveViewer-1.0.1-x86_64.AppImage" \
#     --artifact-file="dist/linux/x86_64/packages/CaveViewer-1.0.1-x86_64.AppImage" \
#     --notes="Bug fixes and performance improvements"

print_usage() {
  cat <<'EOF'
Usage:
  update_manifest.sh --version=<version> --download-url=<url> --artifact-file=<path> [--notes=<release_notes>] [--channel=<stable|preview>]
EOF
}

version=""
appimage_url=""
appimage_file=""
release_notes=""
channel="stable"
while [ "$#" -gt 0 ]; do
  case "$1" in
    --version=*) version="${1#--version=}" ; shift ;;
    --version)
      shift
      if [ "$#" -eq 0 ]; then echo "Error: --version requires a value."; exit 1; fi
      version="$1"
      shift
      ;;
    --download-url=*) appimage_url="${1#--download-url=}" ; shift ;;
    --download-url)
      shift
      if [ "$#" -eq 0 ]; then echo "Error: --download-url requires a value."; exit 1; fi
      appimage_url="$1"
      shift
      ;;
    --artifact-file=*) appimage_file="${1#--artifact-file=}" ; shift ;;
    --artifact-file)
      shift
      if [ "$#" -eq 0 ]; then echo "Error: --artifact-file requires a value."; exit 1; fi
      appimage_file="$1"
      shift
      ;;
    --notes=*) release_notes="${1#--notes=}" ; shift ;;
    --notes)
      shift
      if [ "$#" -eq 0 ]; then echo "Error: --notes requires a value."; exit 1; fi
      release_notes="$1"
      shift
      ;;
    --channel=*) channel="${1#--channel=}" ; shift ;;
    --channel)
      shift
      if [ "$#" -eq 0 ]; then echo "Error: --channel requires a value."; exit 1; fi
      channel="$1"
      shift
      ;;
    -h|--help)
      print_usage
      exit 0
      ;;
    -*)
      echo "Error: unknown option '$1'"
      print_usage
      exit 1
      ;;
    *)
      echo "Error: positional arguments are not supported: '$1'"
      print_usage
      exit 1
      ;;
  esac
done

if [ -z "$version" ] || [ -z "$appimage_url" ] || [ -z "$appimage_file" ]; then
  echo "Error: --version, --download-url, and --artifact-file are required."
  print_usage
  exit 1
fi

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "$script_dir/../../.." && pwd)"

linux_update_arch="${CAVEVIEWER_LINUX_UPDATE_ARCH:-}"
case "$linux_update_arch" in
  amd64|x86|x86_64) manifest_arch_dir="x86_64" ;;
  "")
    case "$(uname -m)" in
      x86_64|amd64) manifest_arch_dir="x86_64" ;;
      *)
        echo "Error: could not determine Linux update manifest architecture. Set CAVEVIEWER_LINUX_UPDATE_ARCH=x86_64."
        exit 1
        ;;
    esac
    ;;
  *)
    echo "Error: invalid CAVEVIEWER_LINUX_UPDATE_ARCH '$linux_update_arch' (expected x86_64)"
    exit 1
    ;;
esac

case "$channel" in
  stable|preview) ;;
  *)
    echo "Error: invalid --channel '$channel' (expected stable or preview)"
    exit 1
    ;;
esac

manifest_path="$repo_root/updates/linux/$manifest_arch_dir/$channel.json"

if command -v python3 >/dev/null 2>&1; then
  manifest_python="python3"
elif command -v python >/dev/null 2>&1; then
  manifest_python="python"
else
  echo "Error: Python 3 is required to write an update manifest." >&2
  exit 1
fi

"$manifest_python" "$repo_root/scripts/write_update_manifest.py" \
  --target linux \
  --version "$version" \
  --download-url "$appimage_url" \
  --artifact-file "$appimage_file" \
  --notes "$release_notes" \
  --channel "$channel" \
  --output "$manifest_path"
