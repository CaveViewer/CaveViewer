#!/usr/bin/env bash
set -euo pipefail

print_usage() {
  cat <<'EOF'
Usage:
  preview_release_automation.sh --source-branch=<branch> \
    --release-notes=<notes>

Verify that one source branch has already reached main, publish the next
all-platform Preview from release/next, and leave the generated release
metadata there for a maintainer-managed pull request into main. This helper is
intended for the Preview Release Promotion GitHub workflow.
EOF
}

source_branch=""
release_notes=""
release_branch="release/next"
main_branch="main"

while [ "$#" -gt 0 ]; do
  case "$1" in
    --source-branch=*) source_branch="${1#--source-branch=}" ;;
    --release-notes=*) release_notes="${1#--release-notes=}" ;;
    -h|--help) print_usage; exit 0 ;;
    *) echo "Error: unknown argument '$1'" >&2; print_usage >&2; exit 2 ;;
  esac
  shift
done

if [ -z "$source_branch" ] || [ -z "$release_notes" ]; then
  echo "Error: --source-branch and --release-notes are required." >&2
  exit 2
fi
if [ "$source_branch" = "$main_branch" ] || [ "$source_branch" = "$release_branch" ]; then
  echo "Error: select a feature branch, not $source_branch." >&2
  exit 2
fi

for command in gh git jq python3; do
  if ! command -v "$command" >/dev/null 2>&1; then
    echo "Error: required command is unavailable: $command" >&2
    exit 1
  fi
done

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "$script_dir/../.." && pwd)"
workflow_waiter="$script_dir/dispatch_workflow_and_wait.sh"
version_selector="$script_dir/next_release_version.py"
repo="${GITHUB_REPOSITORY:-}"
if [ -z "$repo" ]; then
  repo="$(gh repo view --json nameWithOwner --jq .nameWithOwner)"
fi

git -C "$repo_root" fetch --no-tags origin \
  "refs/heads/$main_branch:refs/remotes/origin/$main_branch" \
  "refs/heads/$release_branch:refs/remotes/origin/$release_branch" \
  "refs/heads/$source_branch:refs/remotes/origin/$source_branch"

# Do not stack another release on metadata that has not reached main. This
# check happens before release/next changes so a failed run is side-effect free
# and can be resumed after a maintainer reconciles the previous release.
if ! git -C "$repo_root" diff --quiet "origin/$main_branch" "origin/$release_branch"; then
  echo "Error: $release_branch contains changes not reconciled with $main_branch." >&2
  echo "Merge the existing release metadata PR before starting another Preview." >&2
  exit 1
fi

# Source promotion is intentionally outside release automation. Accept a
# feature branch only when its current remote tip is already reachable from
# protected main, proving that a maintainer-managed PR supplied the source.
source_sha="$(git -C "$repo_root" rev-parse "origin/$source_branch^{commit}")"
if ! git -C "$repo_root" merge-base --is-ancestor \
  "$source_sha" "origin/$main_branch"; then
  echo "Error: origin/$source_branch at $source_sha is not present in origin/$main_branch." >&2
  echo "Merge the source through a maintainer-managed PR before starting a Preview release." >&2
  exit 1
fi
echo "Verified source $source_branch at $source_sha is present in $main_branch."

git -C "$repo_root" fetch --no-tags origin \
  "refs/heads/$main_branch:refs/remotes/origin/$main_branch" \
  "refs/heads/$release_branch:refs/remotes/origin/$release_branch"
git -C "$repo_root" switch -C "$release_branch" "origin/$release_branch"
git -C "$repo_root" merge --no-edit "origin/$main_branch"
git -C "$repo_root" push origin "HEAD:refs/heads/$release_branch"
release_source_sha="$(git -C "$repo_root" rev-parse HEAD)"

current_version="$(
  python3 - "$repo_root/src/caveviewer/version.py" <<'PY'
import re
import sys
from pathlib import Path

text = Path(sys.argv[1]).read_text(encoding="utf-8")
match = re.search(r'^APP_VERSION = "([^"]+)"$', text, re.MULTILINE)
if match is None:
    raise SystemExit("APP_VERSION is missing")
print(match.group(1))
PY
)"
next_version="$(
  {
    printf '%s\n' "$current_version"
    gh api --paginate "repos/$repo/releases?per_page=100" --jq '.[].tag_name'
    gh api --paginate "repos/$repo/tags?per_page=100" --jq '.[].name'
  } | python3 "$version_selector"
)"
echo "Publishing Preview v$next_version from $release_branch at $release_source_sha."

"$workflow_waiter" \
  --workflow=all-platform-release.yml \
  --ref="$release_branch" \
  --head-sha="$release_source_sha" \
  --field="version=$next_version" \
  --field="release_notes=$release_notes" \
  --field="preview=true" \
  --field="publish=true" \
  --field="reuse_pr_validation=true" >/dev/null

git -C "$repo_root" fetch --no-tags origin \
  "refs/heads/$release_branch:refs/remotes/origin/$release_branch"
metadata_sha="$(git -C "$repo_root" rev-parse "origin/$release_branch")"
release_url="https://github.com/$repo/releases/tag/v$next_version"
compare_url="https://github.com/$repo/compare/$main_branch...$release_branch?expand=1"

summary="$(cat <<EOF
Preview v$next_version is published.
Release: $release_url
Source SHA: $release_source_sha
Metadata commit: $metadata_sha
Main remains unchanged. Review and manually merge release/next into main:
$compare_url
EOF
)"
printf '%s\n' "$summary"
if [ -n "${GITHUB_STEP_SUMMARY:-}" ]; then
  {
    echo "## Preview v$next_version published"
    echo
    echo "- Release: [$release_url]($release_url)"
    echo "- Source SHA: \`$release_source_sha\`"
    echo "- Metadata commit: \`$metadata_sha\`"
    echo
    echo "Main was not changed. [Open the manual release metadata PR]($compare_url)."
  } >> "$GITHUB_STEP_SUMMARY"
fi

echo "Release automation completed without creating or merging a pull request."
