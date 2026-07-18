#!/usr/bin/env sh
set -eu
ROOT="$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)"
cd "$ROOT/upstream/litellm"
git fetch --tags
TAG="${1:-v1.90.5}"
git checkout "$TAG"
echo "upstream/litellm now at $(git rev-parse HEAD) ($TAG)"
