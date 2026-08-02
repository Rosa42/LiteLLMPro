#!/usr/bin/env sh
# Upgrade LiteLLM pin (manual gate). Does not auto-pick latest/dev/rc.
set -eu
ROOT="$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

TARGET="${1:-}"
if [ -z "$TARGET" ]; then
  echo "Usage: $0 vX.Y.Z"
  echo "Only stable tags. Refuse: latest main nightly rc dev"
  exit 1
fi
case "$TARGET" in
  *latest*|*main*|*nightly*|*rc*|*dev*)
    echo "Refusing unstable tag: $TARGET"
    exit 1
    ;;
esac

echo "1) working tree check"
git status --porcelain | head
echo "2) current pin"
grep LITELLM_VERSION config/versions.env || true
echo "3) update versions.env to $TARGET (edit + submodule checkout required)"
echo "   git -C upstream/litellm fetch --tags"
echo "   git -C upstream/litellm checkout $TARGET"
echo "   set LITELLM_VERSION=$TARGET in config/versions.env"
echo "4) run tests: pytest tests/unit tests/contract -q"
echo "5) rebuild: docker compose --env-file .env -f deploy/docker-compose.yaml --profile core up -d --build"
echo "6) smoke: sh scripts/smoke-test.sh"
echo "See docs/upgrades.md"
