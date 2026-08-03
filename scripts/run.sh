#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

if [ ! -x .venv/bin/python ]; then
  echo ".venv is missing. Run ./scripts/bootstrap_pi.sh first." >&2
  exit 1
fi

exec .venv/bin/python -m daum_market_guard "$@"
