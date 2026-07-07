#!/bin/bash

# This script sets up the environment for running the CaveViewer application on macOS.

# Set environment variables if needed
export CAVEVIEWER_HOME="$(pwd)"
export PYTHONPATH="$CAVEVIEWER_HOME"

# Optional: configure update checks.
# Explicit manifest URL (highest priority):
# export CAVEVIEWER_UPDATE_MANIFEST_URL="https://raw.githubusercontent.com/KernalPanic/CaveViewerPlus/main/updates/macos/stable.json"
# Or set a repo/branch and let the app derive the platform-specific manifest URL:
export CAVEVIEWER_GITHUB_REPO="KernalPanic/CaveViewerPlus"
# export CAVEVIEWER_UPDATE_BRANCH="feature/pubkey"
# export CAVEVIEWER_UPDATE_CHANNEL="prerelease"

# You can add additional environment configurations here as necessary

echo "CaveViewer environment setup complete."
