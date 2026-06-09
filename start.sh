#!/usr/bin/env bash
cd "$(dirname "$0")/backend"
echo "Starting Nexus Player..."
if command -v xdg-open >/dev/null 2>&1; then
  xdg-open "http://localhost:8899" 2>/dev/null &
elif command -v open >/dev/null 2>&1; then
  open "http://localhost:8899" 2>/dev/null &
fi
python3 app.py