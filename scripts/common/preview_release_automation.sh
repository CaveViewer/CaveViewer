#!/usr/bin/env bash
set -euo pipefail

print_usage() {
  cat <<'EOF'
Usage:
  preview_release_automation.sh --main-sha=<commit> \
    --channel=<preview|stable> --bump=<patch|minor|major> \
    [--version=<version>] \
    --release-notes=<notes>

Verify the exact protected main revision, publish the selected all-platform
release from release/next, and create the generated-metadata pull request into
main. This helper is intended for the Release Promotion GitHub workflow.
EOF
}

main_sha=""
channel=""
bump=""
requested_version=""
release_notes=""
release_branch="release/next"
main_branch="main"

while [ "$#" -gt 0 ]; do
  case "$1" in
    --main-sha=*) main_sha="${1#--main-sha=}" ;;
    --channel=*) channel="${1#--channel=}" ;;
    --bump=*) bump="${1#--bump=}" ;;
    --version=*) requested_version="${1#--version=}" ;;
    --release-notes=*) release_notes="${1#--release-notes=}" ;;
    -h|--help) print_usage; exit 0 ;;
    *) echo "Error: unknown argument '$1'" >&2; print_usage >&2; exit 2 ;;
  esac
  shift
done

if [ -z "$main_sha" ] || [ -z "$channel" ] || [ -z "$bump" ] || [ -z "$release_notes" ]; then
  echo "Error: --main-sha, --channel, --bump, and --release-notes are required." >&2
  exit 2
fi
if [[ ! "$main_sha" =~ ^[0-9a-f]{40}$ ]]; then
  echo "Error: --main-sha must be a full lowercase commit SHA." >&2
  exit 2
fi
if [ "$channel" != "preview" ] && [ "$channel" != "stable" ]; then
  echo "Error: --channel must be preview or stable." >&2
  exit 2
fi
if [ "$bump" != "patch" ] && [ "$bump" != "minor" ] && [ "$bump" != "major" ]; then
  echo "Error: --bump must be patch, minor, or major." >&2
  exit 2
fi
if [ -z "${RELEASE_PR_TOKEN:-}" ]; then
  echo "Error: RELEASE_PR_TOKEN is required to create the metadata pull request." >&2
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
  "refs/heads/$release_branch:refs/remotes/origin/$release_branch"

# Do not stack another release on metadata that has not reached main. This
# check happens before release/next changes so a failed run is side-effect free
# and can be resumed after a maintainer reconciles the previous release.
if ! git -C "$repo_root" merge-base --is-ancestor \
  "origin/$release_branch" "origin/$main_branch"; then
  echo "Error: $release_branch contains changes not reconciled with $main_branch." >&2
  echo "Merge the existing release metadata PR before starting another release." >&2
  exit 1
fi

remote_main_sha="$(git -C "$repo_root" rev-parse "origin/$main_branch^{commit}")"
if [ "$remote_main_sha" != "$main_sha" ]; then
  echo "Error: origin/$main_branch changed ($main_sha requested, $remote_main_sha current)." >&2
  echo "Update your local main and start the release again." >&2
  exit 1
fi
echo "Verified protected $main_branch at $main_sha."

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
calculated_version="$(
  {
    printf '%s\n' "$current_version"
    gh api --paginate "repos/$repo/releases?per_page=100" --jq '.[].tag_name'
    gh api --paginate "repos/$repo/tags?per_page=100" --jq '.[].name'
  } | python3 "$version_selector" --bump "$bump"
)"
next_version="$calculated_version"
if [ -n "$requested_version" ] && [ "$requested_version" != "$calculated_version" ]; then
  echo "Error: requested v$requested_version, but the next $bump version is v$calculated_version." >&2
  exit 1
fi
if [ -n "$requested_version" ]; then
  next_version="$requested_version"
fi
if [ "$channel" = "preview" ]; then
  preview="true"
  channel_label="Preview"
else
  preview="false"
  channel_label="Stable"
fi

git -C "$repo_root" fetch --no-tags origin \
  "refs/heads/$main_branch:refs/remotes/origin/$main_branch" \
  "refs/heads/$release_branch:refs/remotes/origin/$release_branch"
remote_main_sha="$(git -C "$repo_root" rev-parse "origin/$main_branch^{commit}")"
if [ "$remote_main_sha" != "$main_sha" ]; then
  echo "Error: origin/$main_branch advanced before promotion; start again." >&2
  exit 1
fi
git -C "$repo_root" switch -C "$release_branch" "origin/$release_branch"
git -C "$repo_root" merge --ff-only "origin/$main_branch"
git -C "$repo_root" push origin "HEAD:refs/heads/$release_branch"
release_source_sha="$(git -C "$repo_root" rev-parse HEAD)"
echo "Publishing $channel_label v$next_version from $release_branch at $release_source_sha."

"$workflow_waiter" \
  --workflow=all-platform-release.yml \
  --ref="$release_branch" \
  --head-sha="$release_source_sha" \
  --field="version=$next_version" \
  --field="release_notes=$release_notes" \
  --field="preview=$preview" \
  --field="publish=true" >/dev/null

git -C "$repo_root" fetch --no-tags origin \
  "refs/heads/$release_branch:refs/remotes/origin/$release_branch"
metadata_sha="$(git -C "$repo_root" rev-parse "origin/$release_branch")"
release_url="https://github.com/$repo/releases/tag/v$next_version"
metadata_pr_url="$(
  GH_TOKEN="$RELEASE_PR_TOKEN" gh pr list \
    --repo "$repo" \
    --base "$main_branch" \
    --head "$release_branch" \
    --state open \
    --json url \
    --jq '.[0].url // empty'
)"
if [ -z "$metadata_pr_url" ]; then
  metadata_pr_url="$(
    GH_TOKEN="$RELEASE_PR_TOKEN" gh pr create \
      --repo "$repo" \
      --base "$main_branch" \
      --head "$release_branch" \
      --title "Merge release metadata for v$next_version" \
      --body "Merge the published $channel release metadata for v$next_version from release/next."
  )"
fi

summary="$(cat <<EOF
$channel_label v$next_version is published.
Release: $release_url
Source SHA: $release_source_sha
Metadata commit: $metadata_sha
Main remains unchanged. Review the release metadata pull request:
$metadata_pr_url
EOF
)"
printf '%s\n' "$summary"
if [ -n "${GITHUB_STEP_SUMMARY:-}" ]; then
  {
    echo "## $channel_label v$next_version published"
    echo
    echo "- Release: [$release_url]($release_url)"
    echo "- Source SHA: \`$release_source_sha\`"
    echo "- Metadata commit: \`$metadata_sha\`"
    echo
    echo "Main was not changed. [Review the release metadata PR]($metadata_pr_url)."
  } >> "$GITHUB_STEP_SUMMARY"
fi

echo "Release automation created or reused the metadata PR and did not merge it."
