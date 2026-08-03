#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 is required" >&2
  exit 1
fi

if command -v apt-get >/dev/null 2>&1; then
  echo "Installing Raspberry Pi browser/font packages if needed."
  sudo apt-get update
  sudo apt-get install -y python3-full python3-venv fonts-noto-cjk fonts-nanum fontconfig
  if ! command -v chromium-browser >/dev/null 2>&1 && ! command -v chromium >/dev/null 2>&1; then
    sudo apt-get install -y chromium-browser || sudo apt-get install -y chromium
  fi
  fc-cache -f >/dev/null 2>&1 || true
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
