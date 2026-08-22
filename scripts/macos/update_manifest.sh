#!/usr/bin/env bash
set -euo pipefail

# macOS update manifest writer.
# Writes updates/macos/<arch>/<channel>.json with version, download URL,
# package size, SHA-256, and release notes for the in-app update client.
#
# Usage:
#   update_manifest.sh [--arch=<arm64|x86_64>] --version=<version> --download-url=<macos_dmg_url> --artifact-file=<macos_dmg_file> [--notes=<release_notes>] [--channel=<stable|preview>]
# Example:
#   update_manifest.sh --arch=arm64 --version=1.0.1 \
#     --download-url="https://github.com/<owner>/CaveViewerPlus/releases/download/v1.0.1/CaveViewer-1.0.1-macos-arm64.dmg" \
#     --artifact-file="dist/macos/packages/CaveViewer-1.0.1-macos-arm64.dmg" \
#     --notes="Bug fixes and performance improvements"

print_usage() {
  cat <<'EOF'
Usage:
  update_manifest.sh [--arch=<arm64|x86_64>] --version=<version> --download-url=<url> --artifact-file=<path> [--notes=<release_notes>] [--channel=<stable|preview>]
EOF
}

version=""
macos_arch=""
macos_dmg_url=""
macos_dmg_file=""
release_notes=""
channel="stable"
while [ "$#" -gt 0 ]; do
  case "$1" in
    --arch=*) macos_arch="${1#--arch=}" ; shift ;;
    --arch)
      shift
      if [ "$#" -eq 0 ]; then echo "Error: --arch requires a value."; exit 1; fi
      macos_arch="$1"
      shift
      ;;
    --version=*) version="${1#--version=}" ; shift ;;
    --version)
      shift
      if [ "$#" -eq 0 ]; then echo "Error: --version requires a value."; exit 1; fi
      version="$1"
      shift
      ;;
    --download-url=*) macos_dmg_url="${1#--download-url=}" ; shift ;;
    --download-url)
      shift
      if [ "$#" -eq 0 ]; then echo "Error: --download-url requires a value."; exit 1; fi
      macos_dmg_url="$1"
      shift
      ;;
    --artifact-file=*) macos_dmg_file="${1#--artifact-file=}" ; shift ;;
    --artifact-file)
      shift
      if [ "$#" -eq 0 ]; then echo "Error: --artifact-file requires a value."; exit 1; fi
      macos_dmg_file="$1"
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

if [ -z "$version" ] || [ -z "$macos_dmg_url" ] || [ -z "$macos_dmg_file" ]; then
  echo "Error: --version, --download-url, and --artifact-file are required."
  print_usage
  exit 1
fi

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "$script_dir/../.." && pwd)"
source "$script_dir/architecture.sh"
macos_arch="$(cv_resolve_macos_arch "$macos_arch")"
case "$channel" in
  stable|preview) ;;
  *)
    echo "Error: invalid --channel '$channel' (expected stable or preview)"
    exit 1
    ;;
esac
manifest_path="$repo_root/updates/macos/$macos_arch/$channel.json"

if command -v python3 >/dev/null 2>&1; then
  manifest_python="python3"
elif command -v python >/dev/null 2>&1; then
  manifest_python="python"
else
  echo "Error: Python 3 is required to write an update manifest." >&2
  exit 1
fi

"$manifest_python" "$repo_root/scripts/write_update_manifest.py" \
  --target macos \
  --architecture "$macos_arch" \
  --version "$version" \
  --download-url "$macos_dmg_url" \
  --artifact-file "$macos_dmg_file" \
  --notes "$release_notes" \
  --channel "$channel" \
  --output "$manifest_path"
