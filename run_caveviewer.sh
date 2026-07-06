#!/usr/bin/env bash
set -euo pipefail
repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
venv_dir="${CAVEVIEWER_DEV_VENV:-$repo_root/.venv-dev}"

is_virtual_machine() {
	if command -v systemd-detect-virt >/dev/null 2>&1; then
		systemd-detect-virt --quiet --vm && return 0
	fi

	local dmi_value
	for dmi_path in /sys/class/dmi/id/product_name /sys/class/dmi/id/sys_vendor; do
		if [ -r "$dmi_path" ]; then
			dmi_value="$(tr '[:upper:]' '[:lower:]' < "$dmi_path" 2>/dev/null || true)"
			case "$dmi_value" in
				*parallels*|*vmware*|*virtualbox*|*qemu*|*kvm*|*hyper-v*|*bhyve*)
					return 0
					;;
			esac
		fi
	done

	return 1
}

if [ ! -x "$venv_dir/bin/python" ]; then
	echo "Error: python not found at $venv_dir/bin/python"
	echo "Run ./scripts/dev/install.sh to set up dependencies."
	exit 1
fi

if [ -z "${CAVEVIEWER_VSYNC+x}" ] && is_virtual_machine; then
	export CAVEVIEWER_VSYNC=0
	echo "Detected virtual machine; defaulting CAVEVIEWER_VSYNC=0."
fi

cd "$repo_root"
exec "$venv_dir/bin/python" "$repo_root/caveviewer.py" "$@"
