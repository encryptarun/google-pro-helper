#!/bin/zsh
set -euo pipefail

SCRIPT_DIR="${0:A:h}"
cd "$SCRIPT_DIR"

if [[ ! -x .venv/bin/python ]]; then
  print -u2 "Setup is missing. Run 1_install_and_start.command first."
  exit 1
fi

open "http://127.0.0.1:8765"
exec .venv/bin/python app.py
