#!/bin/bash
cd "$(dirname "$0")" || exit 1
export PYTHONIOENCODING=utf-8
printf '\n▶ Setting up BlueStacks Air on macOS\n'
python3 setup_env.py
status=$?
printf '\nPress Enter to close...'
read -r _
exit "$status"
