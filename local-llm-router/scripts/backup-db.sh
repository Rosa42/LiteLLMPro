#!/usr/bin/env sh
# Backup Postgres when using profile full.
set -eu
ROOT="$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)"
mkdir -p "$ROOT/backups"
OUT="$ROOT/backups/litellm_$(date +%Y%m%d_%H%M%S).dump"
echo "Writing $OUT"
docker compose --env-file "$ROOT/.env" -f "$ROOT/deploy/docker-compose.yaml" --profile full \
  exec -T postgres pg_dump -U "${POSTGRES_USER:-litellm}" -d "${POSTGRES_DB:-litellm}" -F c >"$OUT"
echo "OK $OUT"
