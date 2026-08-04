#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
profile="$(pwd)/browser-profile"

if ! command -v pgrep >/dev/null 2>&1; then
  echo "pgrep is required" >&2
  exit 1
fi

mapfile -t pids < <(pgrep -f -- "--user-data-dir=$profile" || true)
if [ "${#pids[@]}" -eq 0 ]; then
  echo "No Chromium process is using $profile"
  exit 0
fi

printf "Stopping Chromium processes using %s: %s\n" "$profile" "${pids[*]}"
kill "${pids[@]}" 2>/dev/null || true
sleep 2

mapfile -t remaining < <(pgrep -f -- "--user-data-dir=$profile" || true)
if [ "${#remaining[@]}" -gt 0 ]; then
  printf "Force stopping remaining processes: %s\n" "${remaining[*]}"
  kill -9 "${remaining[@]}" 2>/dev/null || true
fi

echo "Done."
