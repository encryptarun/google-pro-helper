#!/bin/bash
cd "$(dirname "$0")" || exit 1
export PYTHONIOENCODING=utf-8
export ADB_PATH=/Applications/BlueStacks.app/Contents/MacOS/hd-adb
if [ ! -x .venv/bin/python ]; then
  printf '❌ Run setup_environment.command first.\n'
  read -r _
  exit 1
fi
printf '\n▶ Checking BlueStacks Air, Frida, and billing module\n'
.venv/bin/python pro.py --check
status=$?
printf '\nPress Enter to close...'
read -r _
exit "$status"
