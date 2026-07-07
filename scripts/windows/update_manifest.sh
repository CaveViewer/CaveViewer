#!/usr/bin/env bash
set -euo pipefail

# Writes updates/windows/<channel>.json for the in-app updater.
# Usage:
#   ./scripts/windows/update_manifest.sh --version=<version> --download-url=<windows_zip_url> --artifact-file=<windows_zip_file> [--notes=<release_notes>] [--channel=<stable|prerelease>]
# Example:
#   ./scripts/windows/update_manifest.sh --version=1.0.1 \
#     --download-url="https://github.com/<owner>/CaveViewerPlus/releases/download/v1.0.1/CaveViewer-1.0.1-windows.zip" \
#     --artifact-file="dist/windows/packages/CaveViewer-1.0.1-windows.zip" \
#     --notes="Bug fixes and performance improvements"

print_usage() {
  cat <<'EOF'
Usage:
  update_manifest.sh --version=<version> --download-url=<url> --artifact-file=<path> [--notes=<release_notes>] [--channel=<stable|prerelease>]
EOF
}

version=""
windows_zip_url=""
windows_zip_file=""
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
    --download-url=*) windows_zip_url="${1#--download-url=}" ; shift ;;
    --download-url)
      shift
      if [ "$#" -eq 0 ]; then echo "Error: --download-url requires a value."; exit 1; fi
      windows_zip_url="$1"
      shift
      ;;
    --artifact-file=*) windows_zip_file="${1#--artifact-file=}" ; shift ;;
    --artifact-file)
      shift
      if [ "$#" -eq 0 ]; then echo "Error: --artifact-file requires a value."; exit 1; fi
      windows_zip_file="$1"
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

if [ -z "$version" ] || [ -z "$windows_zip_url" ] || [ -z "$windows_zip_file" ]; then
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
manifest_path="$repo_root/updates/windows/$channel.json"

windows_zip_size_bytes="null"
windows_zip_sha256_value=""

if [ -n "$windows_zip_file" ]; then
  if [ ! -f "$windows_zip_file" ]; then
    echo "Error: Windows zip file not found: $windows_zip_file"
    exit 1
  fi
  windows_zip_size_bytes="$(cv_size_bytes "$windows_zip_file")"
  windows_zip_sha256_value="$(cv_sha256 "$windows_zip_file")"
fi

mkdir -p "$(dirname "$manifest_path")"
cat > "$manifest_path" <<EOF
{
  "latest_version": "$version",
  "download_url": "$windows_zip_url",
  "download_size_bytes": $windows_zip_size_bytes,
  "download_url_windows_zip": "$windows_zip_url",
  "download_size_bytes_windows_zip": $windows_zip_size_bytes,
  "release_notes": "$release_notes",
  "sha256": "$windows_zip_sha256_value",
  "sha256_windows_zip": "$windows_zip_sha256_value"
}
EOF

echo "Wrote manifest: $manifest_path"
