#!/usr/bin/env bash
set -euo pipefail

# Validate a packaged macOS DMG on a matching native runner before publication.

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "$script_dir/../.." && pwd)"
# shellcheck source=scripts/macos/architecture.sh
source "$script_dir/architecture.sh"

print_usage() {
  cat <<'EOF'
Usage:
  smoke_dmg.sh --arch=<arm64|x86_64> --version=<version>
  smoke_dmg.sh --help

Mounts and validates the canonical CaveViewer macOS DMG and metadata for the
selected architecture. Run it on a matching native macOS process.
EOF
}

macos_arch=""
version=""

while [ "$#" -gt 0 ]; do
  case "$1" in
    --arch=*)
      macos_arch="${1#--arch=}"
      shift
      ;;
    --arch)
      shift
      if [ "$#" -eq 0 ]; then
        echo "Error: --arch requires a value." >&2
        exit 1
      fi
      macos_arch="$1"
      shift
      ;;
    --version=*)
      version="${1#--version=}"
      shift
      ;;
    --version)
      shift
      if [ "$#" -eq 0 ]; then
        echo "Error: --version requires a value." >&2
        exit 1
      fi
      version="$1"
      shift
      ;;
    -h|--help)
      print_usage
      exit 0
      ;;
    *)
      echo "Error: unknown option '$1'." >&2
      print_usage >&2
      exit 1
      ;;
  esac
done

case "$macos_arch" in
  arm64|x86_64) ;;
  "")
    echo "Error: --arch is required." >&2
    exit 1
    ;;
  *)
    echo "Error: unsupported macOS architecture '$macos_arch'." >&2
    exit 1
    ;;
esac

if [ -z "$version" ]; then
  echo "Error: --version is required." >&2
  exit 1
fi

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "Error: macOS DMG smoke tests must run on macOS." >&2
  exit 1
fi

cv_require_macos_host_arch "$macos_arch"

artifact_name="CaveViewer-${version}-macos-${macos_arch}.dmg"
metadata_name="CaveViewer-${version}-macos-${macos_arch}.json"
dmg="$repo_root/dist/macos/packages/$artifact_name"
metadata="$repo_root/dist/macos/metadata/$metadata_name"

if [ ! -f "$dmg" ]; then
  echo "Error: DMG not found: $dmg" >&2
  exit 1
fi

if [ ! -f "$metadata" ]; then
  echo "Error: metadata not found: $metadata" >&2
  exit 1
fi

python3 - "$dmg" "$metadata" "$version" "$macos_arch" <<'PY'
import hashlib
import json
import pathlib
import sys

dmg = pathlib.Path(sys.argv[1])
metadata = pathlib.Path(sys.argv[2])
version = sys.argv[3]
architecture = sys.argv[4]
payload = json.loads(metadata.read_text(encoding="utf-8"))

hasher = hashlib.sha256()
with dmg.open("rb") as handle:
    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
        hasher.update(chunk)

artifact_name = f"CaveViewer-{version}-macos-{architecture}.dmg"
expected = {
    "app_name": "CaveViewer",
    "version": version,
    "platform": "macos",
    "architecture": architecture,
    "artifact_file": artifact_name,
    "artifact_path": f"dist/macos/packages/{artifact_name}",
    "sha256": hasher.hexdigest(),
    "size_bytes": dmg.stat().st_size,
}
for key, expected_value in expected.items():
    actual = payload.get(key)
    if actual != expected_value:
        raise SystemExit(
            f"{metadata}: {key} is {actual!r}, expected {expected_value!r}"
        )
PY

mount_dir="$(mktemp -d /tmp/caveviewer_dmg_smoke.XXXXXX)"
attached=0
detach_dmg() {
  if [ "$attached" = "1" ]; then
    if hdiutil detach "$mount_dir" -quiet; then
      attached=0
      return 0
    fi
    if hdiutil detach "$mount_dir" -force -quiet; then
      attached=0
      return 0
    fi
    return 1
  fi
  return 0
}

cleanup() {
  detach_dmg || true
  rm -rf "$mount_dir"
}
trap cleanup EXIT

attach_dmg() {
  local attempt=1
  local max_attempts=5
  local delay_seconds=5
  local status=0

  while [ "$attempt" -le "$max_attempts" ]; do
    if hdiutil attach "$dmg" -nobrowse -readonly -mountpoint "$mount_dir" >/dev/null; then
      return 0
    fi
    status=$?

    if [ "$attempt" -eq "$max_attempts" ]; then
      echo "Error: hdiutil attach failed after $max_attempts attempts." >&2
      return "$status"
    fi

    echo "hdiutil attach failed; retrying in ${delay_seconds}s (attempt $attempt/$max_attempts)." >&2
    sleep "$delay_seconds"
    attempt=$((attempt + 1))
    delay_seconds=$((delay_seconds * 2))
  done
}

attach_dmg
attached=1

app="$mount_dir/CaveViewer.app"
info="$app/Contents/Info.plist"
executable="$app/Contents/MacOS/CaveViewer"

test -d "$app"
test -f "$info"
test -x "$executable"
test -f "$mount_dir/README.md"
test -f "$mount_dir/LICENSE"
test -f "$mount_dir/THIRD_PARTY_NOTICES.md"
test -L "$mount_dir/Applications"

/usr/libexec/PlistBuddy -c "Print :CFBundleName" "$info" | grep -Fx "CaveViewer"
/usr/libexec/PlistBuddy -c "Print :CFBundleDisplayName" "$info" | grep -Fx "CaveViewer"
/usr/libexec/PlistBuddy -c "Print :CFBundleShortVersionString" "$info" | grep -Fx "$version"
/usr/libexec/PlistBuddy -c "Print :CFBundleVersion" "$info" | grep -Fx "$version"

if ! lipo -archs "$executable" | tr ' ' '\n' | grep -Fxq "$macos_arch"; then
  echo "Error: main executable does not contain $macos_arch code." >&2
  exit 1
fi

mach_o_count=0
while IFS= read -r -d '' candidate; do
  if ! file -b "$candidate" | grep -q "Mach-O"; then
    continue
  fi

  mach_o_count=$((mach_o_count + 1))
  if ! lipo -archs "$candidate" | tr ' ' '\n' | grep -Fxq "$macos_arch"; then
    echo "Error: bundled Mach-O file lacks $macos_arch code: $candidate" >&2
    exit 1
  fi

  if otool -L "$candidate" 2>/dev/null | tail -n +2 \
    | grep -E '/Users/runner/|/opt/homebrew/|/usr/local/(Cellar|opt)/' >/dev/null; then
    echo "Error: bundled Mach-O file references a runner-local library: $candidate" >&2
    otool -L "$candidate" >&2
    exit 1
  fi
done < <(find "$app/Contents" -type f -print0)

if [ "$mach_o_count" -eq 0 ]; then
  echo "Error: no Mach-O files found in the application bundle." >&2
  exit 1
fi

set +e
cli_output="$("$executable" --update-branch 2>&1)"
cli_status=$?
set -e
if [ "$cli_status" -ne 2 ]; then
  echo "Error: packaged CLI smoke expected exit 2, got $cli_status." >&2
  echo "$cli_output" >&2
  exit 1
fi
if ! grep -Fq "Error:" <<<"$cli_output"; then
  echo "Error: packaged CLI smoke did not report a controlled error." >&2
  echo "$cli_output" >&2
  exit 1
fi

if ! detach_dmg; then
  echo "Warning: unable to detach DMG mount cleanly after successful validation: $mount_dir" >&2
fi

echo "Validated macOS $macos_arch DMG: $artifact_name"
