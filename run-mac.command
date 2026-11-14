#!/bin/sh
# wxbrief on macOS. Double-click or run in Terminal.
cd "$(dirname "$0")/content" || exit 1
exec python3 -u app.py
