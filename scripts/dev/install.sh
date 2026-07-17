#!/usr/bin/env bash
set -euo pipefail

# Development environment installer.
# Creates/updates the local virtual environment and writes run_caveviewer.sh.
#
# Usage:
#   install.sh
#   install.sh --help

print_usage() {
  cat <<'EOF'
Usage:
  install.sh
  install.sh --help

Creates or updates the CaveViewer development virtual environment.
EOF
}

while [ "$#" -gt 0 ]; do
  case "$1" in
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
      echo ""
      print_usage
      exit 1
      ;;
  esac
done

# Location helpers (script can be run from anywhere)
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# The script has a fixed home under scripts/dev; validate the package root so
# a copied/partial script cannot install from the wrong directory.
project_root="$(cd "$script_dir/../.." && pwd)"
source "$script_dir/../common/python.sh"
if [ ! -f "$project_root/pyproject.toml" ] \
  || [ ! -f "$project_root/src/caveviewer/__main__.py" ]; then
  echo "Error: could not find the CaveViewer package relative to install.sh."
  echo "Run install.sh from the project root: ./scripts/dev/install.sh"
  exit 1
fi

echo "Setting up CaveViewer (project root: $project_root)"

python_bin="$(cv_resolve_project_python)"

# Create development virtual environment
legacy_venv_dir="$project_root/.venv"
venv_dir="${CAVEVIEWER_DEV_VENV:-$project_root/.venv-dev}"
venv_python="$venv_dir/bin/python"

if [ "$venv_dir" = "$project_root/.venv-dev" ] \
  && [ ! -e "$venv_dir" ] \
  && [ -d "$legacy_venv_dir" ] \
  && [ -x "$legacy_venv_dir/bin/python" ] \
  && cv_python_is_supported "$legacy_venv_dir/bin/python"; then
  echo "Migrating existing development virtual environment: $legacy_venv_dir -> $venv_dir"
  mv "$legacy_venv_dir" "$venv_dir"
fi

if [ ! -x "$venv_python" ] || ! cv_python_is_supported "$venv_python"; then
  if [ -d "$venv_dir" ]; then
    echo "Existing virtual environment at $venv_dir is invalid; recreating it."
    rm -rf "$venv_dir"
  fi
  echo "Creating virtual environment at $venv_dir"
  "$python_bin" -m venv "$venv_dir"
fi

echo "Using development virtual environment: $venv_dir"

# Upgrade pip and install dependencies from provided requirements.txt
echo "Installing Python packages from requirements.txt"
"$venv_python" -m pip install --upgrade pip
"$venv_python" -m pip install -r "$project_root/requirements.txt"
"$venv_python" -m pip install --no-deps -e "$project_root"

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
exec "$venv_dir/bin/python" -m caveviewer "$@"
EOF
chmod +x "$launcher_path"

echo "Setup complete."
echo "Run CaveViewer with: $launcher_path"
