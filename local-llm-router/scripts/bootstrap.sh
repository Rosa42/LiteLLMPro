#!/usr/bin/env sh
# Local bootstrap helper (M0).
set -eu
ROOT="$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if [ ! -f .env ]; then
  cp .env.example .env
  echo "Created .env from .env.example — edit secrets before starting."
fi

echo "LITELLM_VERSION from config/versions.env:"
grep -E '^LITELLM_VERSION=' config/versions.env || true

echo "Validating compose (core profile)..."
docker compose --env-file .env -f deploy/docker-compose.yaml --profile core config >/dev/null
echo "compose config OK"

echo "Next:"
echo "  1. Edit .env (LITELLM_MASTER_KEY >= 32 bytes, REDIS_PASSWORD)"
echo "  2. docker compose --env-file .env -f deploy/docker-compose.yaml --profile core up -d --build"
