#!/usr/bin/env bash
set -euo pipefail

# Windows update manifest writer.
# Writes updates/windows/<channel>.json with version, download URL, package
# size, SHA-256, and release notes for the in-app update client.
#
# Usage:
#   update_manifest.sh --version=<version> --download-url=<windows_package_url> --artifact-file=<windows_package_file> --authenticode-certificate-subject=<subject> [--notes=<release_notes>] [--channel=<stable|prerelease>]
# Example:
#   update_manifest.sh --version=1.0.1 \
#     --download-url="https://github.com/<owner>/CaveViewer/releases/download/v1.0.1/CaveViewer-1.0.1-windows.exe" \
#     --artifact-file="dist/windows/packages/CaveViewer-1.0.1-windows.exe" \
#     --notes="Bug fixes and performance improvements"

print_usage() {
  cat <<'EOF'
Usage:
  update_manifest.sh --version=<version> --download-url=<url> --artifact-file=<path> --authenticode-certificate-subject=<subject> [--notes=<release_notes>] [--channel=<stable|prerelease>]
EOF
}

version=""
windows_package_url=""
windows_package_file=""
release_notes=""
channel="stable"
authenticode_certificate_subject=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --version=*) version="${1#--version=}" ; shift ;;
    --version)
      shift
      if [ "$#" -eq 0 ]; then echo "Error: --version requires a value."; exit 1; fi
      version="$1"
      shift
      ;;
    --download-url=*) windows_package_url="${1#--download-url=}" ; shift ;;
    --download-url)
      shift
      if [ "$#" -eq 0 ]; then echo "Error: --download-url requires a value."; exit 1; fi
      windows_package_url="$1"
      shift
      ;;
    --artifact-file=*) windows_package_file="${1#--artifact-file=}" ; shift ;;
    --artifact-file)
      shift
      if [ "$#" -eq 0 ]; then echo "Error: --artifact-file requires a value."; exit 1; fi
      windows_package_file="$1"
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
    --authenticode-certificate-subject=*) authenticode_certificate_subject="${1#--authenticode-certificate-subject=}" ; shift ;;
    --authenticode-certificate-subject)
      shift
      if [ "$#" -eq 0 ]; then echo "Error: --authenticode-certificate-subject requires a value."; exit 1; fi
      authenticode_certificate_subject="$1"
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

if [ -z "$version" ] || [ -z "$windows_package_url" ] || [ -z "$windows_package_file" ] || [ -z "$authenticode_certificate_subject" ]; then
  echo "Error: --version, --download-url, --artifact-file, and --authenticode-certificate-subject are required."
  print_usage
  exit 1
fi

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "$script_dir/../.." && pwd)"
case "$channel" in
  stable|prerelease) ;;
  *)
    echo "Error: invalid --channel '$channel' (expected stable or prerelease)"
    exit 1
    ;;
esac
manifest_path="$repo_root/updates/windows/$channel.json"

if command -v python3 >/dev/null 2>&1; then
  manifest_python="python3"
elif command -v python >/dev/null 2>&1; then
  manifest_python="python"
else
  echo "Error: Python 3 is required to write an update manifest." >&2
  exit 1
fi

"$manifest_python" "$repo_root/scripts/write_update_manifest.py" \
  --target windows \
  --version "$version" \
  --download-url "$windows_package_url" \
  --artifact-file "$windows_package_file" \
  --notes "$release_notes" \
  --channel "$channel" \
  --authenticode-certificate-subject "$authenticode_certificate_subject" \
  --output "$manifest_path"
