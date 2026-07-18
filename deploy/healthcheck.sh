#!/usr/bin/env sh
# Basic health probe for local smoke checks.
set -eu
HOST="${1:-127.0.0.1}"
PORT="${2:-4000}"
URL="http://${HOST}:${PORT}/health/liveliness"
echo "Checking ${URL}"
# Prefer curl; fall back to python
if command -v curl >/dev/null 2>&1; then
  curl -fsS "${URL}"
else
  python -c "import urllib.request; print(urllib.request.urlopen('${URL}', timeout=5).read().decode())"
fi
echo
echo "OK"
