# CaveViewer Development From Source

This guide is for users who want to run CaveViewer from source.

Scope:

- This document is intentionally focused on source-based development and local runs.

## Get Source Files

You can start in either of these ways:

- Clone the repository with Git (recommended for contributors).
- Download source files from GitHub and unpack them locally.

The repository's source archive format is:

- `CaveViewer-<version>-source.tar.gz`

This format is produced by the existing source packaging flow in `scripts/common/package_source.sh`.

Release packages should include:

- `LICENSE`
- `THIRD_PARTY_NOTICES.md`

The application About text should identify CaveViewer as licensed under the GNU General Public License version 3.0.

## Requirements

- Git
- Python 3.10+

## Clone the Repository

```bash
git clone https://github.com/KernalPanic/CaveViewerPlus.git
cd CaveViewerPlus
```

Optional: check out the latest version tag.

```bash
git fetch --tags
latest=$(git tag -l "v*" --sort=-version:refname | head -n 1)
git checkout "$latest"
```

## Optional: Make This Local Clone Read-Only (No Push)

If you only want to pull and run from source, you can disable push behavior in this clone.

```bash
# 1) Disable default pushes from this repo
git config --local push.default nothing

# 2) Set an intentionally invalid push URL for origin
git remote set-url --push origin DISABLED

# 3) Verify read-only push setup
git config --local --get push.default
git remote -v
```

Expected result:

- `push.default` shows `nothing`
- `origin (push)` shows `DISABLED`

With this setup, normal pull/fetch operations continue to work, but push attempts fail immediately.

## macOS and Linux: Run From Source

Use the project bootstrap script:

```bash
./scripts/dev/install.sh
```

What it does:

- Creates a development virtual environment at `.venv-dev` (or `CAVEVIEWER_DEV_VENV` if set)
- Installs dependencies from `requirements.txt`
- Generates `run_caveviewer.sh`

Run the app:

```bash
./run_caveviewer.sh
```

## Windows: Run From Source

Option A (recommended for technical users): manual venv flow.

```powershell
py -3 -m venv .venv-dev
.\.venv-dev\Scripts\python -m pip install --upgrade pip
.\.venv-dev\Scripts\python -m pip install -r requirements.txt
.\.venv-dev\Scripts\python caveviewer.py
```

Option B (guided setup script in this repo):

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\windows\setup.ps1
```

Notes:

- `scripts/windows/setup.ps1` is designed to install prerequisites and set up a runnable local source environment.
- `scripts/windows/launch.bat` is a launcher for the setup script.

## Sample Map Source Overrides

By default, the built-in sample maps dialog reads release assets from:

- Repository: `KernalPanic/CaveViewer`
- Release tag: `sample-data`

For local development, you can point the sample maps dialog at a different source before launching the program. These settings are environment variables only; they are not exposed in the app UI.

Precedence:

1. `CAVEVIEWER_SAMPLE_MAPS_API_URL` uses a full release API URL directly.
2. Otherwise, CaveViewer builds the GitHub release API URL from `CAVEVIEWER_SAMPLE_MAPS_REPO` and `CAVEVIEWER_SAMPLE_DATA_TAG`.
3. If none are set, the defaults above are used.

macOS/Linux example:

```bash
CAVEVIEWER_SAMPLE_MAPS_REPO="MyOrg/MyMaps" \
CAVEVIEWER_SAMPLE_DATA_TAG="public-samples" \
./run_caveviewer.sh
```

Windows PowerShell example:

```powershell
$env:CAVEVIEWER_SAMPLE_MAPS_REPO = "MyOrg/MyMaps"
$env:CAVEVIEWER_SAMPLE_DATA_TAG = "public-samples"
.\.venv-dev\Scripts\python caveviewer.py
```

Advanced direct API override:

```bash
CAVEVIEWER_SAMPLE_MAPS_API_URL="https://api.github.com/repos/MyOrg/MyMaps/releases/tags/public-samples" \
./run_caveviewer.sh
```

The API response must be compatible with GitHub's release API shape, including an `assets` list with asset `name`, `browser_download_url`, and `size` fields.

## Updating Your Local Source Environment

When the repository changes:

```bash
git pull --ff-only
```

Then refresh dependencies in your active dev venv:

```bash
.venv-dev/bin/python -m pip install -r requirements.txt
```

On Windows (PowerShell):

```powershell
.\.venv-dev\Scripts\python -m pip install -r requirements.txt
```

## Troubleshooting

- `python3 not found` (macOS/Linux): install Python 3.10+ and rerun setup.
- Broken `.venv-dev`: remove it and rerun `./scripts/dev/install.sh`.
- Windows PowerShell policy blocks setup script: run with `-ExecutionPolicy Bypass` as shown above.
