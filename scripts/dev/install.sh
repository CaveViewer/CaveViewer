#!/usr/bin/env bash
set -euo pipefail

# Location helpers (script can be run from anywhere)
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Detect project root: prefer the directory containing caveviewer.py.
project_root=""
if [ -f "$script_dir/caveviewer.py" ]; then
  project_root="$script_dir"
elif [ -f "$script_dir/../caveviewer.py" ]; then
  project_root="$(cd "$script_dir/.." && pwd)"
elif [ -f "$script_dir/../../caveviewer.py" ]; then
  project_root="$(cd "$script_dir/../.." && pwd)"
else
  echo "Error: could not find caveviewer.py relative to install.sh."
  echo "Run install.sh from the project root: ./scripts/dev/install.sh"
  exit 1
fi

echo "Setting up CaveViewer (project root: $project_root)"

# Ensure python3 exists (attempt brew install if missing)
if ! command -v python3 >/dev/null 2>&1; then
  echo "Error: python3 not found. Install Python 3.10+ and re-run."
  exit 1
fi

# Create development virtual environment
legacy_venv_dir="$project_root/.venv"
venv_dir="${CAVEVIEWER_DEV_VENV:-$project_root/.venv-dev}"
venv_python="$venv_dir/bin/python"

if [ "$venv_dir" = "$project_root/.venv-dev" ] \
  && [ ! -e "$venv_dir" ] \
  && [ -d "$legacy_venv_dir" ] \
  && [ -x "$legacy_venv_dir/bin/python" ] \
  && "$legacy_venv_dir/bin/python" -c "import sys" >/dev/null 2>&1; then
  echo "Migrating existing development virtual environment: $legacy_venv_dir -> $venv_dir"
  mv "$legacy_venv_dir" "$venv_dir"
fi

if [ ! -x "$venv_python" ] || ! "$venv_python" -c "import sys" >/dev/null 2>&1; then
  if [ -d "$venv_dir" ]; then
    echo "Existing virtual environment at $venv_dir is invalid; recreating it."
    rm -rf "$venv_dir"
  fi
  echo "Creating virtual environment at $venv_dir"
  python3 -m venv "$venv_dir"
fi

echo "Using development virtual environment: $venv_dir"

# Upgrade pip and install dependencies from provided requirements.txt
echo "Installing Python packages from requirements.txt"
"$venv_python" -m pip install --upgrade pip
"$venv_python" -m pip install -r "$project_root/requirements.txt"

# Create a small launcher that always uses this project's virtualenv.
launcher_path="$project_root/run_caveviewer.sh"
echo "Creating launcher script: $launcher_path"
cat > "$launcher_path" <<'EOF'
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
EOF
chmod +x "$launcher_path"

echo "Setup complete."
echo "Run CaveViewer with: $launcher_path"
