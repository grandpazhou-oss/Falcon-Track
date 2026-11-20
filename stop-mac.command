#!/bin/bash
cd "$(dirname "$0")" || exit 1
if [ ! -x .venv/bin/python ]; then
  printf '%s\n' "Run setup-mac.command first."
else
  .venv/bin/python scripts/local.py stop
fi
read -r -p "Press Enter to close..." _
