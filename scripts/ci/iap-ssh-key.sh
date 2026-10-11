#!/usr/bin/env bash
set -euo pipefail

usage() {
  echo "usage: $0 register <repo-name> <zone> | run <state-dir> <timeout-seconds> <repo-name> <zone> | wait <state-dir> <timeout-seconds>" >&2
  exit 2
}

valid_seconds() {
  [[ "$1" =~ ^[1-9][0-9]{0,2}$ ]] && (( 10#$1 <= 300 ))
}

register_key() {
  local repo_name=$1 zone=$2 status
  # The first SSH creates the runner's project metadata key and waits for it
  # to reach the notebook VM.
  gcloud compute ssh "${repo_name}-notebook" --zone "$zone" \
    --tunnel-through-iap --command true

  # Check the workers VM too: gcloud's first-SSH propagation wait covers only
  # the VM it connected to above.
  status=$(gcloud compute instances describe "${repo_name}-workers" \
    --zone "$zone" --format='value(status)')
  if [[ "$status" == RUNNING ]]; then
    for attempt in $(seq 1 6); do
      if gcloud compute ssh "${repo_name}-workers" --zone "$zone" \
        --tunnel-through-iap --command true; then
        return 0
      fi
      if [[ "$attempt" == 6 ]]; then
        echo "workers VM did not accept the SSH key" >&2
        return 1
      fi
      sleep 10
    done
  fi
}

recorded_run() {
  local state_dir=$1 timeout_s=$2 repo_name=$3 zone=$4
  local rc status_tmp
  mkdir -p "$state_dir"
  rm -f "$state_dir/status" "$state_dir/status.tmp"
  set +e
  timeout --signal=TERM --kill-after=5 "${timeout_s}s" "$0" register "$repo_name" "$zone" \
    >"$state_dir/registration.log" 2>&1
  rc=$?
  set -e
  status_tmp="$state_dir/status.tmp"
  printf '%s\n' "$rc" > "$status_tmp"
  mv "$status_tmp" "$state_dir/status"
}

wait_for_run() {
  local state_dir=$1 timeout_s=$2 status elapsed
  for ((elapsed = 0; elapsed < timeout_s; elapsed++)); do
    if [[ -e "$state_dir/status" ]]; then
      status=$(<"$state_dir/status")
      if ! [[ "$status" =~ ^(0|[1-9][0-9]{0,2})$ ]] || (( 10#$status > 255 )); then
        echo "IAP SSH registration status is incomplete or invalid" >&2
        [[ ! -f "$state_dir/registration.log" ]] || cat "$state_dir/registration.log" >&2
        return 1
      fi
      [[ ! -f "$state_dir/registration.log" ]] || cat "$state_dir/registration.log"
      if (( status != 0 )); then
        echo "IAP SSH registration failed (exit $status)" >&2
        return "$status"
      fi
      return 0
    fi
    sleep 1
  done
  echo "IAP SSH registration status missing after ${timeout_s}s" >&2
  [[ ! -f "$state_dir/registration.log" ]] || cat "$state_dir/registration.log" >&2
  return 1
}

case "${1:-}" in
  register)
    [[ $# == 3 ]] || usage
    register_key "$2" "$3"
    ;;
  run)
    [[ $# == 5 ]] && valid_seconds "$3" || usage
    recorded_run "$2" "$3" "$4" "$5"
    ;;
  wait)
    [[ $# == 3 ]] && valid_seconds "$3" || usage
    wait_for_run "$2" "$3"
    ;;
  *)
    usage
    ;;
esac
