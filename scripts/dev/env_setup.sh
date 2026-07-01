#!/bin/bash

# This script sets up the environment for running the CaveViewer application on macOS.

# Set environment variables if needed
export CAVEVIEWER_HOME="$(pwd)"
export PYTHONPATH="$CAVEVIEWER_HOME"

# Optional: configure update checks.
# Explicit manifest URL (highest priority):
# export CAVEVIEWER_UPDATE_MANIFEST_URL="https://raw.githubusercontent.com/KernalPanic/CaveViewerPlus/main/updates/macos/stable.json"
# Or set a repo and let the app derive:
export CAVEVIEWER_GITHUB_REPO="KernalPanic/CaveViewerPlus"

# You can add additional environment configurations here as necessary

echo "CaveViewer environment setup complete."