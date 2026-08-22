#!/usr/bin/env bash
set -euo pipefail

print_usage() {
  cat <<'EOF'
Usage:
  reconcile_release_metadata.sh --version=<version> --channel=<stable|preview>

Open or reuse the release/next metadata pull request, validate it with the
required Essential Tests workflow, and merge it into protected main.
EOF
}

version=""
channel=""
release_branch="release/next"
main_branch="main"

while [ "$#" -gt 0 ]; do
  case "$1" in
    --version=*) version="${1#--version=}" ;;
    --channel=*) channel="${1#--channel=}" ;;
    -h|--help) print_usage; exit 0 ;;
    *) echo "Error: unknown argument '$1'" >&2; print_usage >&2; exit 2 ;;
  esac
  shift
done

if [ -z "$version" ] || { [ "$channel" != "stable" ] && [ "$channel" != "preview" ]; }; then
  echo "Error: --version and --channel=<stable|preview> are required." >&2
  exit 2
fi

for command in gh git jq; do
  if ! command -v "$command" >/dev/null 2>&1; then
    echo "Error: required command is unavailable: $command" >&2
    exit 1
  fi
done

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "$script_dir/../.." && pwd)"
workflow_waiter="$script_dir/dispatch_workflow_and_wait.sh"
repo="${GITHUB_REPOSITORY:-}"
if [ -z "$repo" ]; then
  repo="$(gh repo view --json nameWithOwner --jq .nameWithOwner)"
fi

git -C "$repo_root" fetch --no-tags origin \
  "refs/heads/$main_branch:refs/remotes/origin/$main_branch" \
  "refs/heads/$release_branch:refs/remotes/origin/$release_branch"

if git -C "$repo_root" diff --quiet "origin/$main_branch" "origin/$release_branch"; then
  echo "Release metadata is already reconciled with $main_branch."
  exit 0
fi

pr_number="$(
  gh pr list \
    --repo "$repo" \
    --head "$release_branch" \
    --base "$main_branch" \
    --state open \
    --json number \
    --jq '.[0].number // empty'
)"
if [ -z "$pr_number" ]; then
  gh pr create \
    --repo "$repo" \
    --head "$release_branch" \
    --base "$main_branch" \
    --title "Release v$version $channel metadata" \
    --body "Automated reconciliation of signed $channel v$version release metadata." >/dev/null
  pr_number="$(
    gh pr list \
      --repo "$repo" \
      --head "$release_branch" \
      --base "$main_branch" \
      --state open \
      --json number \
      --jq '.[0].number // empty'
  )"
fi
if [ -z "$pr_number" ]; then
  echo "Error: could not resolve the $release_branch metadata pull request." >&2
  exit 1
fi

pr_base_sha="$(gh pr view "$pr_number" --repo "$repo" --json baseRefOid --jq .baseRefOid)"
pr_head_sha="$(gh pr view "$pr_number" --repo "$repo" --json headRefOid --jq .headRefOid)"
echo "Validating release metadata PR #$pr_number."
"$workflow_waiter" \
  --workflow=tests.yml \
  --ref="$release_branch" \
  --head-sha="$pr_head_sha" \
  --field="pr_base_sha=$pr_base_sha" \
  --field="pr_head_sha=$pr_head_sha" >/dev/null

gh pr merge "$pr_number" \
  --repo "$repo" \
  --merge \
  --delete-branch=false

echo "Release v$version $channel metadata is merged into $main_branch."
