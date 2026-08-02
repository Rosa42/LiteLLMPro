#!/usr/bin/env sh
set -eu
ROOT="$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
PREV="${1:-v1.90.5}"
echo "Rollback to $PREV"
echo "- restore config/versions.env LITELLM_VERSION=$PREV"
echo "- git -C upstream/litellm checkout $PREV"
echo "- docker compose --env-file .env -f deploy/docker-compose.yaml --profile core up -d --build"
echo "- Keep LITELLM_SALT_KEY unchanged if using encrypted DB credentials"
echo "- smoke: sh scripts/smoke-test.sh"
echo "See docs/rollback.md"
