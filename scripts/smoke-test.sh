#!/usr/bin/env sh
set -eu
ROOT="$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)"
HOST="${SMOKE_HOST:-127.0.0.1}"
PORT="${SMOKE_PORT:-4000}"
BASE="http://${HOST}:${PORT}"
KEY="${LITELLM_MASTER_KEY:-change-me-to-a-random-value-at-least-32-bytes-long}"

echo "== liveliness =="
curl -fsS "${BASE}/health/liveliness" || curl -fsS "${BASE}/health"
echo
echo "== models =="
curl -fsS -H "Authorization: Bearer ${KEY}" "${BASE}/v1/models"
echo
echo "== chat (non-stream) =="
curl -fsS -H "Authorization: Bearer ${KEY}" -H "Content-Type: application/json" \
  -d '{"model":"kimi-k3","messages":[{"role":"user","content":"ping"}],"max_tokens":8}' \
  "${BASE}/v1/chat/completions" | head -c 500
echo
echo "smoke OK"
