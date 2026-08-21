#!/usr/bin/env bash
set -euo pipefail

# Build the single Windows installer release artifact from the frozen payload.
# Production packages fail closed unless Authenticode signing is available or a
# deliberately selected unsigned community-release policy is in effect.

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "$script_dir/../.." && pwd)"
source "$repo_root/scripts/common/artifacts.sh"
source "$repo_root/scripts/common/python.sh"
source "$repo_root/scripts/common/release_channel.sh"
source "$repo_root/scripts/common/version.sh"

print_usage() {
  cat <<'EOF'
Usage:
  package.sh [--base-download-url=<url>]
  package.sh --help

Builds CaveViewer-<version>-windows.exe from the frozen Windows payload.

The default production contract requires both
CAVEVIEWER_WINDOWS_SIGNING_CERTIFICATE_SUBJECT and
CAVEVIEWER_WINDOWS_TIMESTAMP_URL. Set
CAVEVIEWER_ALLOW_UNSIGNED_WINDOWS_PACKAGE=1 only for package validation, or
combine it with CAVEVIEWER_WINDOWS_UNSIGNED_RELEASE=community for the explicit
unsigned community-release policy. Release finalization rejects every other
unsigned package status.
EOF
}

is_windows_host() {
  case "${OS:-}:$(uname -s)" in
    Windows_NT:*|*:MINGW*|*:MSYS*|*:CYGWIN*) return 0 ;;
    *) return 1 ;;
  esac
}

windows_path() {
  local path="$1"
  if command -v cygpath >/dev/null 2>&1; then
    cygpath -aw "$path"
  else
    printf '%s\n' "$path"
  fi
}

resolve_powershell() {
  local candidate=""
  for candidate in powershell.exe powershell; do
    if command -v "$candidate" >/dev/null 2>&1; then
      command -v "$candidate"
      return 0
    fi
  done
  echo "Error: PowerShell is required for Windows Authenticode verification." >&2
  return 1
}

resolve_inno_compiler() {
  local requested="${CAVEVIEWER_INNO_SETUP_COMPILER:-}"
  local candidate="" converted=""
  if [ -n "$requested" ]; then
    if [ -x "$requested" ]; then
      printf '%s\n' "$requested"
      return 0
    fi
    if command -v cygpath >/dev/null 2>&1; then
      converted="$(cygpath -u "$requested" 2>/dev/null || true)"
      if [ -n "$converted" ] && [ -x "$converted" ]; then
        printf '%s\n' "$converted"
        return 0
      fi
    fi
    if command -v "$requested" >/dev/null 2>&1; then
      command -v "$requested"
      return 0
    fi
    echo "Error: CAVEVIEWER_INNO_SETUP_COMPILER is not executable: $requested" >&2
    return 1
  fi
  for candidate in ISCC.exe ISCC; do
    if command -v "$candidate" >/dev/null 2>&1; then
      command -v "$candidate"
      return 0
    fi
  done
  echo "Error: Inno Setup 6 compiler (ISCC.exe) is required for Windows packaging." >&2
  echo "Install Inno Setup or set CAVEVIEWER_INNO_SETUP_COMPILER." >&2
  return 1
}

resolve_metadata_python() {
  local candidate="${CAVEVIEWER_WINDOWS_BUILD_PYTHON:-}"
  if [ -z "$candidate" ]; then
    candidate="${CAVEVIEWER_WINDOWS_BUILD_VENV:-$repo_root/.venv-windows-build}/Scripts/python.exe"
  fi
  if ! cv_python_is_supported "$candidate"; then
    echo "Error: metadata generation requires the Python 3.12 Windows build environment: $candidate" >&2
    return 1
  fi
  printf '%s\n' "$candidate"
}

sign_binary() {
  local artifact="$1"
  "$powershell_exe" -NoProfile -ExecutionPolicy Bypass \
    -File "$sign_script_windows" \
    -ArtifactPath "$(windows_path "$artifact")" \
    -CertificateSubject "$certificate_subject" \
    -TimestampUrl "$timestamp_url"
}

verify_signed_binary() {
  local artifact="$1"
  "$powershell_exe" -NoProfile -ExecutionPolicy Bypass \
    -File "$verify_script_windows" \
    -ArtifactPath "$(windows_path "$artifact")" \
    -ExpectedCertificateSubject "$certificate_subject"
}

base_download_url=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --base-download-url=*)
      base_download_url="${1#--base-download-url=}"
      shift
      ;;
    --base-download-url)
      shift
      if [ "$#" -eq 0 ]; then
        echo "Error: --base-download-url requires a value." >&2
        exit 1
      fi
      base_download_url="$1"
      shift
      ;;
    -h|--help)
      print_usage
      exit 0
      ;;
    -*)
      echo "Error: unknown option '$1'" >&2
      print_usage >&2
      exit 1
      ;;
    *)
      echo "Error: positional arguments are not supported: '$1'" >&2
      print_usage >&2
      exit 1
      ;;
  esac
done

if ! is_windows_host; then
  echo "Error: scripts/windows/package.sh requires a native Windows host." >&2
  exit 1
fi

certificate_subject="${CAVEVIEWER_WINDOWS_SIGNING_CERTIFICATE_SUBJECT:-}"
timestamp_url="${CAVEVIEWER_WINDOWS_TIMESTAMP_URL:-}"
allow_unsigned="${CAVEVIEWER_ALLOW_UNSIGNED_WINDOWS_PACKAGE:-0}"
unsigned_release="${CAVEVIEWER_WINDOWS_UNSIGNED_RELEASE:-}"
case "$allow_unsigned" in
  0|1) ;;
  *)
    echo "Error: CAVEVIEWER_ALLOW_UNSIGNED_WINDOWS_PACKAGE must be 0 or 1." >&2
    exit 1
    ;;
esac
case "$unsigned_release" in
  ""|community) ;;
  *)
    echo "Error: CAVEVIEWER_WINDOWS_UNSIGNED_RELEASE must be empty or 'community'." >&2
    exit 1
    ;;
esac

if { [ -n "$certificate_subject" ] && [ -z "$timestamp_url" ]; } || \
  { [ -z "$certificate_subject" ] && [ -n "$timestamp_url" ]; }; then
  echo "Error: set both Windows signing variables or neither." >&2
  exit 1
fi
if [ -n "$certificate_subject" ] && [ "${timestamp_url#https://}" = "$timestamp_url" ]; then
  echo "Error: CAVEVIEWER_WINDOWS_TIMESTAMP_URL must use HTTPS." >&2
  exit 1
fi
if [ -n "$certificate_subject" ] && [ -n "$unsigned_release" ]; then
  echo "Error: Authenticode signing and CAVEVIEWER_WINDOWS_UNSIGNED_RELEASE cannot be combined." >&2
  exit 1
fi
if [ -n "$unsigned_release" ] && [ "$allow_unsigned" != "1" ]; then
  echo "Error: CAVEVIEWER_WINDOWS_UNSIGNED_RELEASE=community requires CAVEVIEWER_ALLOW_UNSIGNED_WINDOWS_PACKAGE=1." >&2
  exit 1
fi

signing_enabled=false
authenticode_status="unsigned-test-only"
if [ -n "$certificate_subject" ]; then
  signing_enabled=true
  authenticode_status="verified"
elif [ "$unsigned_release" = "community" ]; then
  authenticode_status="unsigned-community"
elif [ "$allow_unsigned" != "1" ]; then
  echo "Error: production Windows packages require Authenticode signing." >&2
  echo "Set CAVEVIEWER_WINDOWS_SIGNING_CERTIFICATE_SUBJECT and CAVEVIEWER_WINDOWS_TIMESTAMP_URL on a protected signing host." >&2
  echo "For package validation, set CAVEVIEWER_ALLOW_UNSIGNED_WINDOWS_PACKAGE=1." >&2
  echo "For an explicitly configured unsigned community release, also set CAVEVIEWER_WINDOWS_UNSIGNED_RELEASE=community." >&2
  exit 1
fi

version_file="$repo_root/src/caveviewer/version.py"
installer_script="$repo_root/packaging/windows/CaveViewerSetup.iss"
icon_file="$repo_root/scripts/windows/icon/caveviewer.ico"
sign_script="$script_dir/sign_artifact.ps1"
verify_script="$script_dir/verify_signature.ps1"
metadata_writer="$script_dir/write_package_metadata.py"
packages_dir="$repo_root/dist/windows/packages"
metadata_dir="$repo_root/dist/windows/metadata"
installer_dir="$repo_root/dist/windows/installer"
payload_dir="$repo_root/dist/windows/app/CaveViewer"
release_channel="$(cv_release_channel)"

for required_path in "$version_file" "$installer_script" "$icon_file" "$metadata_writer"; do
  if [ ! -f "$required_path" ]; then
    echo "Error: required Windows package input is missing: $required_path" >&2
    exit 1
  fi
done
if $signing_enabled; then
  for required_path in "$sign_script" "$verify_script"; do
    if [ ! -f "$required_path" ]; then
      echo "Error: required Windows signing helper is missing: $required_path" >&2
      exit 1
    fi
  done
fi

version="$(cv_read_app_version "$version_file")"
app_name="$(cv_read_app_name "$version_file")"
if [ -z "$version" ] || [ -z "$app_name" ]; then
  echo "Error: could not parse APP_NAME/APP_VERSION from $version_file" >&2
  exit 1
fi

"$script_dir/build.sh"
if [ ! -f "$payload_dir/CaveViewer.exe" ]; then
  echo "Error: frozen Windows payload is missing CaveViewer.exe: $payload_dir" >&2
  exit 1
fi

metadata_python="$(resolve_metadata_python)"
inno_compiler="$(resolve_inno_compiler)"
powershell_exe=""
sign_script_windows=""
verify_script_windows=""
if $signing_enabled; then
  powershell_exe="$(resolve_powershell)"
  sign_script_windows="$(windows_path "$sign_script")"
  verify_script_windows="$(windows_path "$verify_script")"

  while IFS= read -r -d '' payload_binary; do
    sign_binary "$payload_binary"
  done < <(
    find "$payload_dir" -type f \( \
      -iname '*.exe' -o -iname '*.dll' -o -iname '*.pyd' \
    \) -print0
  )
fi

bundle_name="${app_name}-${version}"
artifact_name="${bundle_name}-windows.exe"
artifact_path="$packages_dir/$artifact_name"
metadata_path="$metadata_dir/${bundle_name}.json"
update_metadata_path="$metadata_dir/${bundle_name}.update.json"
installer_path="$installer_dir/CaveViewerSetup.exe"
download_url=""
if [ -n "$base_download_url" ]; then
  download_url="${base_download_url%/}/$artifact_name"
fi

mkdir -p "$packages_dir" "$metadata_dir" "$installer_dir"
rm -f "$artifact_path" "$metadata_path" "$update_metadata_path" "$installer_path"

# Use ISCC's dash-prefixed switches. Git Bash/MSYS rewrites slash-prefixed
# arguments such as /D... as POSIX paths before native Windows programs see
# them, which makes ISCC interpret them as additional script filenames.
inno_args=(
  "-DAppVersion=$version"
  "-DPayloadDir=$(windows_path "$payload_dir")"
  "-DOutputDir=$(windows_path "$installer_dir")"
  "-DOutputBaseName=CaveViewerSetup"
  "-DSetupIconFile=$(windows_path "$icon_file")"
)
if $signing_enabled; then
  # Inno Setup expands $f to an already-quoted filename. Do not add another
  # quote layer or paths containing spaces will be passed incorrectly.
  inno_sign_command="$(windows_path "$powershell_exe") -NoProfile -ExecutionPolicy Bypass -File \"$sign_script_windows\" -ArtifactPath \$f -CertificateSubject \"$certificate_subject\" -TimestampUrl \"$timestamp_url\""
  inno_args+=(
    "-DEnableCodeSigning=1"
    "-SCaveViewerSign=$inno_sign_command"
  )
fi
inno_args+=("$(windows_path "$installer_script")")
"$inno_compiler" "${inno_args[@]}"

if [ ! -f "$installer_path" ]; then
  echo "Error: Inno Setup did not create CaveViewerSetup.exe: $installer_path" >&2
  exit 1
fi
if $signing_enabled; then
  verify_signed_binary "$installer_path"
fi

cp "$installer_path" "$artifact_path"
"$metadata_python" "$metadata_writer" \
  --artifact-file "$artifact_path" \
  --metadata-output "$metadata_path" \
  --update-output "$update_metadata_path" \
  --app-name "$app_name" \
  --version "$version" \
  --release-channel "$release_channel" \
  --created-at-utc "$(cv_created_at_utc)" \
  --download-url "$download_url" \
  --authenticode-status "$authenticode_status" \
  --authenticode-certificate-subject "$certificate_subject"
if $signing_enabled; then
  "$metadata_python" "$script_dir/verify_package_metadata.py" \
    --artifact-file "$artifact_path" \
    --metadata-file "$metadata_path" \
    --update-metadata-file "$update_metadata_path" \
    --release-channel "$release_channel"
elif [ "$authenticode_status" = "unsigned-community" ]; then
  "$metadata_python" "$script_dir/verify_package_metadata.py" \
    --artifact-file "$artifact_path" \
    --metadata-file "$metadata_path" \
    --update-metadata-file "$update_metadata_path" \
    --release-channel "$release_channel" \
    --allow-unsigned-community
fi

echo "Packaged Windows installer: $artifact_path"
echo "Installer entrypoint: CaveViewerSetup.exe"
echo "Authenticode status: $authenticode_status"
echo "Metadata file: $metadata_path"
echo "Update metadata: $update_metadata_path"
echo "SHA256: $(cv_sha256 "$artifact_path")"
