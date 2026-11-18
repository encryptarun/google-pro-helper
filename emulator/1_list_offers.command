#!/bin/bash
cd "$(dirname "$0")" || exit 1
export PYTHONIOENCODING=utf-8
export ADB_PATH=/Applications/BlueStacks.app/Contents/MacOS/hd-adb
if [ ! -x .venv/bin/python ]; then
  printf '❌ Run setup_environment.command first.\n'
  read -r _
  exit 1
fi
printf '\n▶ Listing Pro 5 TB offers (no purchase)\n'
.venv/bin/python pro.py --account "current@device" --no-add --force --list --plan pro12
status=$?
printf '\nPress Enter to close...'
read -r _
exit "$status"
