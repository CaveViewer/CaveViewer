#!/usr/bin/env bash
set -euo pipefail

print_usage() {
  cat <<'EOF'
Usage:
  preview_release_automation.sh --source-branch=<branch> \
    --release-notes=<notes>

Promote one source branch through main, publish the next all-platform Preview
from release/next, then merge the generated release metadata back into main.
This helper is intended for the Preview Release Promotion GitHub workflow.
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
metadata_reconciler="$script_dir/reconcile_release_metadata.sh"
repo="${GITHUB_REPOSITORY:-}"
if [ -z "$repo" ]; then
  repo="$(gh repo view --json nameWithOwner --jq .nameWithOwner)"
fi

git -C "$repo_root" fetch --no-tags origin \
  "refs/heads/$main_branch:refs/remotes/origin/$main_branch" \
  "refs/heads/$release_branch:refs/remotes/origin/$release_branch" \
  "refs/heads/$source_branch:refs/remotes/origin/$source_branch"

# Do not stack another release on metadata that has not reached main. This
# check happens before the source PR is merged so a failed run is side-effect
# free and can be resumed after reconciling the previous release.
if ! git -C "$repo_root" diff --quiet "origin/$main_branch" "origin/$release_branch"; then
  echo "Error: $release_branch contains changes not reconciled with $main_branch." >&2
  echo "Merge the existing release metadata PR before starting another Preview." >&2
  exit 1
fi

# Bring the selected source branch up to the current protected base before its
# PR validation. Besides satisfying strict checks, this makes the automation
# usable for branches created before the promotion workflow itself existed.
git -C "$repo_root" switch -C "$source_branch" "origin/$source_branch"
git -C "$repo_root" merge --no-edit "origin/$main_branch"
git -C "$repo_root" push origin "HEAD:refs/heads/$source_branch"

open_or_create_pr() {
  local head_branch="$1"
  local base_branch="$2"
  local title="$3"
  local body="$4"
  local pr_number
  pr_number="$(
    gh pr list \
      --repo "$repo" \
      --head "$head_branch" \
      --base "$base_branch" \
      --state open \
      --json number \
      --jq '.[0].number // empty'
  )"
  if [ -z "$pr_number" ]; then
    gh pr create \
      --repo "$repo" \
      --head "$head_branch" \
      --base "$base_branch" \
      --title "$title" \
      --body "$body" >/dev/null
    pr_number="$(
      gh pr list \
        --repo "$repo" \
        --head "$head_branch" \
        --base "$base_branch" \
        --state open \
        --json number \
        --jq '.[0].number // empty'
    )"
  fi
  if [ -z "$pr_number" ]; then
    echo "Error: could not resolve the pull request for $head_branch." >&2
    exit 1
  fi
  printf '%s\n' "$pr_number"
}

validate_pr() {
  local pr_number="$1"
  local pr_base_sha
  local pr_head_sha
  pr_base_sha="$(gh pr view "$pr_number" --repo "$repo" --json baseRefOid --jq .baseRefOid)"
  pr_head_sha="$(gh pr view "$pr_number" --repo "$repo" --json headRefOid --jq .headRefOid)"
  "$workflow_waiter" \
    --workflow=tests.yml \
    --ref="$(gh pr view "$pr_number" --repo "$repo" --json headRefName --jq .headRefName)" \
    --head-sha="$pr_head_sha" \
    --field="pr_base_sha=$pr_base_sha" \
    --field="pr_head_sha=$pr_head_sha" >/dev/null
}

merge_pr() {
  local pr_number="$1"
  gh pr merge "$pr_number" \
    --repo "$repo" \
    --merge \
    --delete-branch=false
}

source_pr="$(
  open_or_create_pr \
    "$source_branch" \
    "$main_branch" \
    "Promote $source_branch for Preview release" \
    "Automated source promotion for the next all-platform Preview release."
)"
echo "Validating source PR #$source_pr."
validate_pr "$source_pr"
merge_pr "$source_pr"

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
  --field="reconcile_metadata=false" \
  --field="reuse_pr_validation=true" >/dev/null

"$metadata_reconciler" --version="$next_version" --channel=preview

echo "Preview v$next_version is published and its metadata is merged into $main_branch."
