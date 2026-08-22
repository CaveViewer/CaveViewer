#!/usr/bin/env bash
set -euo pipefail

print_usage() {
  cat <<'EOF'
Usage:
  dispatch_workflow_and_wait.sh --workflow=<file> --ref=<branch> \
    --head-sha=<commit> [--field=<name=value> ...]

Dispatch one GitHub Actions workflow, resolve the exact new run for the
requested commit, wait for completion, and print the run database ID.
EOF
}

workflow=""
workflow_ref=""
head_sha=""
fields=()

while [ "$#" -gt 0 ]; do
  case "$1" in
    --workflow=*) workflow="${1#--workflow=}" ;;
    --ref=*) workflow_ref="${1#--ref=}" ;;
    --head-sha=*) head_sha="${1#--head-sha=}" ;;
    --field=*) fields+=("--field" "${1#--field=}") ;;
    -h|--help) print_usage; exit 0 ;;
    *) echo "Error: unknown argument '$1'" >&2; print_usage >&2; exit 2 ;;
  esac
  shift
done

if [ -z "$workflow" ] || [ -z "$workflow_ref" ] || [ -z "$head_sha" ]; then
  echo "Error: --workflow, --ref, and --head-sha are required." >&2
  print_usage >&2
  exit 2
fi

existing_run_ids="$(
  gh run list \
    --workflow "$workflow" \
    --branch "$workflow_ref" \
    --event workflow_dispatch \
    --limit 30 \
    --json databaseId |
    jq '[.[].databaseId]'
)"
gh workflow run "$workflow" --ref "$workflow_ref" "${fields[@]}"

run_id=""
for _attempt in $(seq 1 60); do
  run_id="$(
    gh run list \
      --workflow "$workflow" \
      --branch "$workflow_ref" \
      --event workflow_dispatch \
      --limit 30 \
      --json databaseId,headSha,createdAt |
      jq -r \
        --arg head_sha "$head_sha" \
        --argjson existing_run_ids "$existing_run_ids" \
        '[.[] |
          select(.headSha == $head_sha) |
          select(.databaseId as $id | ($existing_run_ids | index($id) | not))] |
         sort_by(.createdAt) | last | .databaseId // empty'
  )"
  [ -n "$run_id" ] && break
  sleep 5
done

if [ -z "$run_id" ]; then
  echo "Error: could not resolve the dispatched $workflow run for $head_sha." >&2
  exit 1
fi

echo "Waiting for $workflow run $run_id on $workflow_ref ($head_sha)." >&2
gh run watch "$run_id" --exit-status
printf '%s\n' "$run_id"
