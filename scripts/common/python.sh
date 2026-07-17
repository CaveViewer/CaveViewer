#!/usr/bin/env bash

# Shared Python 3.12 discovery and validation for local project tooling.
CV_PYTHON_SERIES="3.12"

cv_python_is_supported() {
  local python_bin="${1:-}"
  [ -n "$python_bin" ] \
    && "$python_bin" -c \
      'import sys; raise SystemExit(sys.version_info[:2] != (3, 12))' \
      >/dev/null 2>&1
}

cv_resolve_project_python() {
  local requested="${CAVEVIEWER_PYTHON:-}"
  local candidate=""

  if [ -n "$requested" ]; then
    if [ -x "$requested" ]; then
      candidate="$requested"
    elif command -v "$requested" >/dev/null 2>&1; then
      candidate="$(command -v "$requested")"
    else
      echo "Error: CAVEVIEWER_PYTHON is not executable or on PATH: $requested" >&2
      return 1
    fi

    if cv_python_is_supported "$candidate"; then
      echo "$candidate"
      return 0
    fi
    echo "Error: CAVEVIEWER_PYTHON must use Python $CV_PYTHON_SERIES: $candidate" >&2
    return 1
  fi

  for candidate in python3.12 python3; do
    if command -v "$candidate" >/dev/null 2>&1; then
      candidate="$(command -v "$candidate")"
      if cv_python_is_supported "$candidate"; then
        echo "$candidate"
        return 0
      fi
    fi
  done

  echo "Error: CaveViewer requires Python $CV_PYTHON_SERIES." >&2
  echo "Install Python $CV_PYTHON_SERIES or set CAVEVIEWER_PYTHON to its executable." >&2
  return 1
}
