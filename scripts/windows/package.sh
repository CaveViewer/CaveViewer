#!/usr/bin/env bash
set -euo pipefail

# Windows package builder.
# Creates the portable Windows source bundle, release metadata, and update
# metadata under dist/windows.
#
# Usage:
#   package.sh [--base-download-url=<url>]

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "$script_dir/../.." && pwd)"
source "$repo_root/scripts/common/version.sh"
source "$repo_root/scripts/common/artifacts.sh"
source "$repo_root/scripts/common/github.sh"

print_usage() {
  cat <<'EOF'
Usage:
  package.sh [--base-download-url=<url>]
  package.sh --help

Builds the Windows portable source bundle and metadata.
EOF
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
				echo "Error: --base-download-url requires a value."
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
			echo "Error: unknown option '$1'"
			echo ""
			print_usage
			exit 1
			;;
		*)
			echo "Error: positional arguments are not supported: '$1'"
			echo "Use --base-download-url=<url>."
			exit 1
			;;
	esac
done

version_file="$repo_root/caveviewer_version.py"
packages_dir="$repo_root/dist/windows/packages"
metadata_dir="$repo_root/dist/windows/metadata"
app_root="$repo_root/dist/windows/app"

if [ ! -f "$version_file" ]; then
	echo "Error: version file not found: $version_file"
	exit 1
fi

cv_require_cmd git
cv_require_cmd python

version="$(cv_read_app_version "$version_file")"
app_name="$(cv_read_app_name "$version_file")"

if [ -z "$version" ] || [ -z "$app_name" ]; then
	echo "Error: could not parse APP_NAME/APP_VERSION from $version_file"
	exit 1
fi

bundle_name="${app_name}-${version}"
artifact_name="${bundle_name}-windows.zip"
artifact_path="$packages_dir/$artifact_name"
meta_name="${bundle_name}.json"
meta_path="$metadata_dir/$meta_name"
update_meta_name="${bundle_name}.update.json"
update_meta_path="$metadata_dir/$update_meta_name"
staging_root="$app_root/$bundle_name"
release_dir="$staging_root/release"

mkdir -p "$packages_dir" "$metadata_dir" "$app_root"
rm -f "$artifact_path" "$meta_path"
rm -f "$update_meta_path"
rm -f "$packages_dir"/*-windows-setup.zip
rm -rf "$staging_root"
mkdir -p "$release_dir"

python - "$repo_root" "$staging_root" <<'PY'
import pathlib
import shutil
import subprocess
import sys

repo_root = pathlib.Path(sys.argv[1])
staging_root = pathlib.Path(sys.argv[2])

pathspecs = [
		"caveviewer.py",
		"caveviewer_version.py",
		"requirements.txt",
		"README.md",
		"LICENSE",
		"THIRD_PARTY_NOTICES.md",
		"CaveViewer.spec",
		"core",
		"gui",
		"shaders",
		"updates",
		"scripts/windows",
]

result = subprocess.run(
		["git", "-C", str(repo_root), "ls-files", "-z", "--", *pathspecs],
		check=True,
		stdout=subprocess.PIPE,
)

files = [path for path in result.stdout.decode("utf-8").split("\0") if path]
for relative_path in files:
		source_path = repo_root / relative_path
		if not source_path.exists():
				continue
		destination_path = staging_root / relative_path
		destination_path.parent.mkdir(parents=True, exist_ok=True)
		shutil.copy2(source_path, destination_path)

for relative_path in ("LICENSE", "THIRD_PARTY_NOTICES.md"):
		source_path = repo_root / relative_path
		if source_path.is_file():
				destination_path = staging_root / relative_path
				destination_path.parent.mkdir(parents=True, exist_ok=True)
				shutil.copy2(source_path, destination_path)

# Generated/updated runtime files may be present in a release workspace before
# they have been committed. Copy required files explicitly so the Windows
# source bundle does not silently omit a helper module or fall back to older
# tracked artwork.
required_runtime_paths = [
		"gui/preferences.py",
		"gui/assets/app_icon_macos.png",
		"gui/assets/app_icon_windows.png",
		"gui/assets/app_mark_transparent.png",
		"scripts/windows/icon/caveviewer.ico",
]
for relative_path in required_runtime_paths:
		source_path = repo_root / relative_path
		if source_path.is_file():
				destination_path = staging_root / relative_path
				destination_path.parent.mkdir(parents=True, exist_ok=True)
				shutil.copy2(source_path, destination_path)

# Make setup scripts first-class citizens at the bundle root.
root_launch = staging_root / "launch.bat"
root_setup = staging_root / "setup.ps1"
root_icon = staging_root / "icon" / "caveviewer.ico"
src_launch = staging_root / "scripts" / "windows" / "launch.bat"
src_setup = staging_root / "scripts" / "windows" / "setup.ps1"
src_icon = staging_root / "scripts" / "windows" / "icon" / "caveviewer.ico"

if not src_launch.is_file() or not src_setup.is_file():
	missing = []
	if not src_launch.is_file():
		missing.append(str(src_launch))
	if not src_setup.is_file():
		missing.append(str(src_setup))
	raise RuntimeError(f"Missing required Windows setup script(s): {', '.join(missing)}")

shutil.copy2(src_launch, root_launch)
shutil.copy2(src_setup, root_setup)
if src_icon.is_file():
	root_icon.parent.mkdir(parents=True, exist_ok=True)
	shutil.copy2(src_icon, root_icon)
PY

python - "$repo_root" "$staging_root" "$artifact_name" "$version" "$app_name" <<'PY'
import hashlib
import json
import pathlib
import sys

repo_root = pathlib.Path(sys.argv[1])
staging_root = pathlib.Path(sys.argv[2])
artifact_name = sys.argv[3]
version = sys.argv[4]
app_name = sys.argv[5]

important_files = [
	"README.md",
	"LICENSE",
	"THIRD_PARTY_NOTICES.md",
	"caveviewer.py",
	"caveviewer_version.py",
	"requirements.txt",
	"scripts/windows/launch.bat",
	"scripts/windows/setup.ps1",
	"scripts/windows/icon/caveviewer.ico",
	"gui/assets/app_icon_macos.png",
	"gui/assets/app_icon_windows.png",
	"gui/assets/app_mark_transparent.png",
	"updates/windows/stable.json",
]

sha_lines = []
for relative_path in important_files:
	path = repo_root / relative_path
	if not path.is_file():
		continue
	hasher = hashlib.sha256()
	with path.open("rb") as file_handle:
		for chunk in iter(lambda: file_handle.read(1024 * 1024), b""):
			hasher.update(chunk)
	sha_lines.append(f"{hasher.hexdigest()}  {relative_path}")

(staging_root / "release" / "SHA256SUMS.txt").write_text("\n".join(sha_lines) + "\n", encoding="utf-8")

release_manifest = {
	"app_name": app_name,
	"version": version,
	"bundle_type": "windows_portable_source_bundle",
	"artifact_file": artifact_name,
	"bundle_contains": [
		"source_files",
		"LICENSE",
		"THIRD_PARTY_NOTICES.md",
		"launch.bat",
		"setup.ps1",
		"release/SHA256SUMS.txt",
		"release/manifest.json",
	],
}

(staging_root / "release" / "manifest.json").write_text(
	json.dumps(release_manifest, indent=2, sort_keys=True) + "\n",
	encoding="utf-8",
)
PY

python - "$app_root" "$staging_root" "$artifact_path" <<'PY'
import pathlib
import sys
import zipfile

app_root = pathlib.Path(sys.argv[1])
staging_root = pathlib.Path(sys.argv[2])
artifact_path = pathlib.Path(sys.argv[3])

with zipfile.ZipFile(artifact_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
		for path in sorted(staging_root.rglob("*")):
				if path.is_file():
						archive.write(path, path.relative_to(app_root).as_posix())
PY

sha256="$(cv_sha256 "$artifact_path")"
size_bytes="$(cv_size_bytes "$artifact_path")"
created_at_utc="$(cv_created_at_utc)"
download_url=""

if [ -n "$base_download_url" ]; then
	download_url="${base_download_url%/}/$artifact_name"
fi

python - "$update_meta_path" "$app_name" "$version" "$download_url" "$size_bytes" "$sha256" <<'PY'
import json
import pathlib
import sys

update_meta_path = pathlib.Path(sys.argv[1])
app_name = sys.argv[2]
version = sys.argv[3]
download_url = sys.argv[4]
size_bytes = int(sys.argv[5])
sha256 = sys.argv[6]

update_manifest = {
	"app_name": app_name,
	"latest_version": version,
	"install_channel": "windows_app",
	"download_url": download_url,
	"download_url_windows_zip": download_url,
	"download_size_bytes": size_bytes,
	"download_size_bytes_windows_zip": size_bytes,
	"sha256": sha256,
	"sha256_windows_zip": sha256,
	"release_notes": "",
}

update_meta_path.write_text(
	json.dumps(update_manifest, indent=2, sort_keys=True) + "\n",
	encoding="utf-8",
)
PY

cat > "$meta_path" <<EOF
{
	"app_name": "$app_name",
	"version": "$version",
	"package_type": "windows_portable_source_bundle",
	"artifact_file": "$artifact_name",
	"artifact_path": "dist/windows/packages/$artifact_name",
	"entrypoint": "launch.bat",
	"sha256": "$sha256",
	"size_bytes": $size_bytes,
	"created_at_utc": "$created_at_utc",
	"download_url": "$download_url"
}
EOF

if [ "${KEEP_WINDOWS_APP_DIR:-0}" != "1" ]; then
	rm -rf "$app_root"
fi

echo "Packaged Windows artifact: $artifact_path"
echo "Metadata file: $meta_path"
echo "Update manifest: $update_meta_path"
echo "SHA256: $sha256"
