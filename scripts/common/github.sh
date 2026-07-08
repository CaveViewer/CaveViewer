#!/usr/bin/env bash

# GitHub helper functions.
# Source this file from release scripts that need command checks or repository
# owner/name inference.

cv_require_cmd() {
  local cmd="$1"
  if ! command -v "$cmd" >/dev/null 2>&1; then
    echo "Error: required command not found: $cmd"
    exit 1
  fi
}

cv_infer_repo() {
  local repo_root="$1"
  local remote
  remote="$(git -C "$repo_root" remote get-url origin 2>/dev/null || true)"
  if [ -z "$remote" ]; then
    return 1
  fi

  if [[ "$remote" =~ ^git@github.com:([^/]+)/([^/]+)(\.git)?$ ]]; then
    echo "${BASH_REMATCH[1]}/${BASH_REMATCH[2]%\.git}"
    return 0
  fi

  if [[ "$remote" =~ ^https://github.com/([^/]+)/([^/]+)(\.git)?$ ]]; then
    echo "${BASH_REMATCH[1]}/${BASH_REMATCH[2]%\.git}"
    return 0
  fi

  return 1
}
