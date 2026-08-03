#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 is required" >&2
  exit 1
fi

if ! python3 -m venv .venv; then
  echo "Failed to create .venv." >&2
  echo "Install venv support first: sudo apt install -y python3-full python3-venv" >&2
  exit 1
fi
. .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt

if command -v chromium-browser >/dev/null 2>&1 || command -v chromium >/dev/null 2>&1; then
  echo "System Chromium found."
else
  echo "No system Chromium found. Installing Playwright Chromium."
  python -m playwright install chromium
fi

if [ ! -f config.toml ]; then
  cp config.example.toml config.toml
  echo "Created config.toml from config.example.toml."
fi

python -m unittest discover -s tests -v
python -m daum_market_guard --help >/dev/null

echo "Bootstrap complete."
echo "Run commands through the venv, for example:"
echo "  ./scripts/run.sh login --config config.toml"
