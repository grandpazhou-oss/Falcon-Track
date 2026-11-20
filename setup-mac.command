#!/bin/bash
cd "$(dirname "$0")" || exit 1
for candidate in python3.12 python3.13 python3.11 python3.14 python3; do
  if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c 'import sys; raise SystemExit(0 if (3,11)<=sys.version_info[:2]<(3,15) else 1)' >/dev/null 2>&1; then
    "$candidate" scripts/local.py setup
    result=$?
    read -r -p "Press Enter to close..." _
    exit "$result"
  fi
done
printf '%s
' 'Python 3.11–3.14 is required. Install Python 3.12 from python.org, then retry.'
read -r -p 'Press Enter to close...' _
exit 1
