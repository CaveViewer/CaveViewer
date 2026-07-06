#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export CAVEVIEWER_LINUX_UPDATE_ARCH=x86_64
exec "$script_dir/../common/publish.sh" "$@"
