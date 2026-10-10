#!/usr/bin/env bash
# Web Modder: first run creates .venv and installs dependencies, then opens the browser.
set -euo pipefail
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ]; then
  python3 -m venv .venv
  .venv/bin/python -m pip install -q --upgrade pip
  .venv/bin/python -m pip install -q -r requirements.txt
fi
exec .venv/bin/python run.py "$@"
