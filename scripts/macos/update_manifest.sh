#!/usr/bin/env bash
set -euo pipefail

# macOS updater manifest writer.
# Writes updates/macos/<channel>.json with version, download URL, package size,
# SHA-256, and release notes for the in-app updater.
#
# Usage:
#   update_manifest.sh --version=<version> --download-url=<macos_dmg_url> --artifact-file=<macos_dmg_file> [--notes=<release_notes>] [--channel=<stable|prerelease>]
# Example:
#   update_manifest.sh --version=1.0.1 \
#     --download-url="https://github.com/<owner>/CaveViewerPlus/releases/download/v1.0.1/CaveViewer-1.0.1.dmg" \
#     --artifact-file="dist/macos/packages/CaveViewer-1.0.1.dmg" \
#     --notes="Bug fixes and performance improvements"

print_usage() {
  cat <<'EOF'
Usage:
  update_manifest.sh --version=<version> --download-url=<url> --artifact-file=<path> [--notes=<release_notes>] [--channel=<stable|prerelease>]
EOF
}

version=""
macos_dmg_url=""
macos_dmg_file=""
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
source "$repo_root/scripts/common/artifacts.sh"
case "$channel" in
  stable|prerelease) ;;
  *)
    echo "Error: invalid --channel '$channel' (expected stable or prerelease)"
    exit 1
    ;;
esac
manifest_path="$repo_root/updates/macos/$channel.json"

macos_dmg_size_bytes="null"
macos_dmg_sha256_value=""

if [ -n "$macos_dmg_file" ]; then
  if [ ! -f "$macos_dmg_file" ]; then
    echo "Error: macOS DMG file not found: $macos_dmg_file"
    exit 1
  fi
  macos_dmg_size_bytes="$(cv_size_bytes "$macos_dmg_file")"
  macos_dmg_sha256_value="$(cv_sha256 "$macos_dmg_file")"
fi

mkdir -p "$(dirname "$manifest_path")"
cat > "$manifest_path" <<EOF
{
  "latest_version": "$version",
  "download_url": "$macos_dmg_url",
  "download_size_bytes": $macos_dmg_size_bytes,
  "download_url_macosx_dmg": "$macos_dmg_url",
  "download_size_bytes_macosx_dmg": $macos_dmg_size_bytes,
  "release_notes": "$release_notes",
  "sha256": "$macos_dmg_sha256_value",
  "sha256_macosx_dmg": "$macos_dmg_sha256_value"
}
EOF

echo "Wrote manifest: $manifest_path"
