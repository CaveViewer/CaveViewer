#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "$script_dir/../.." && pwd)"

: "${RELEASE_VERSION:?RELEASE_VERSION is required}"
: "${RELEASE_PREVIEW:?RELEASE_PREVIEW is required}"
: "${RELEASE_SOURCE_SHA:?RELEASE_SOURCE_SHA is required}"
: "${CAVEVIEWER_GITHUB_REPO:?CAVEVIEWER_GITHUB_REPO is required}"

published_tags="$(
  gh api --paginate \
    "repos/$CAVEVIEWER_GITHUB_REPO/releases?per_page=100" \
    --jq '.[] | select(.draft == false) | .tag_name'
)"
classification="$(
  printf '%s\n' "$published_tags" |
    python3 "$script_dir/validate_release_version.py" \
      --candidate "$RELEASE_VERSION"
)"

tag="v$RELEASE_VERSION"
verify_tag_source() {
  git -C "$repo_root" fetch --no-tags origin \
    "+refs/tags/$tag:refs/tags/$tag"
  local tag_source_sha
  tag_source_sha="$(git -C "$repo_root" rev-list -n 1 "$tag")"
  if [ "$tag_source_sha" != "$RELEASE_SOURCE_SHA" ]; then
    echo "Error: $tag belongs to source $tag_source_sha, not $RELEASE_SOURCE_SHA." >&2
    exit 1
  fi
}

if [ "$classification" = "new" ]; then
  if git -C "$repo_root" ls-remote --exit-code origin \
    "refs/tags/$tag" >/dev/null 2>&1; then
    verify_tag_source
  fi
  echo "Validated new release v$RELEASE_VERSION."
  exit 0
fi

existing_preview="$(
  gh release view "$tag" --repo "$CAVEVIEWER_GITHUB_REPO" \
    --json isPrerelease --jq .isPrerelease
)"
if [ "$existing_preview" != "$RELEASE_PREVIEW" ]; then
  echo "Error: $tag exists on a different release channel." >&2
  exit 1
fi

verify_tag_source

echo "Validated exact-version resume of $tag from $RELEASE_SOURCE_SHA."
