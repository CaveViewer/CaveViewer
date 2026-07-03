#!/usr/bin/env bash
set -euo pipefail
repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
venv_dir="${CAVEVIEWER_DEV_VENV:-$repo_root/.venv-dev}"

if [ ! -x "$venv_dir/bin/python" ]; then
	echo "Error: python not found at $venv_dir/bin/python"
	echo "Run ./scripts/dev/install.sh to set up dependencies."
	exit 1
fi

cd "$repo_root"
exec "$venv_dir/bin/python" "$repo_root/caveviewer.py"
