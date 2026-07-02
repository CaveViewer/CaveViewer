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

# Create virtual environment
venv_dir="$project_root/.venv"
venv_python="$venv_dir/bin/python"
if [ ! -x "$venv_python" ] || ! "$venv_python" -c "import sys" >/dev/null 2>&1; then
  if [ -d "$venv_dir" ]; then
    echo "Existing virtual environment at $venv_dir is invalid; recreating it."
    rm -rf "$venv_dir"
  fi
  echo "Creating virtual environment at $venv_dir"
  python3 -m venv "$venv_dir"
fi

# Upgrade pip and install dependencies from provided requirements.txt
echo "Installing Python packages from requirements.txt"
"$venv_python" -m pip install --upgrade pip
"$venv_python" -m pip install -r "$project_root/requirements.txt"

# Create a small launcher that always uses this project's virtualenv.
launcher_path="$project_root/run_caveviewer.sh"
echo "Creating launcher script: $launcher_path"
cat > "$launcher_path" <<EOF
#!/usr/bin/env bash
set -euo pipefail
cd "$project_root"
exec "$venv_dir/bin/python" "$project_root/caveviewer.py"
EOF
chmod +x "$launcher_path"

echo "Setup complete."
echo "Run CaveViewer with: $launcher_path"