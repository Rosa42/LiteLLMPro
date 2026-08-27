"""SHA-256 file cache for visual translations. Not stored in Redis ``sq:*``."""

from __future__ import annotations

import os
from pathlib import Path

SCHEMA_VER = 3

_CACHE_NAME = "vision-cache"


def vision_cache_dir() -> Path:
    raw = (os.environ.get("GATEWAY_VISION_CACHE_DIR") or "").strip()
    if raw:
        return Path(raw)
    from shared_quota_router.data_paths import default_data_dir

    return default_data_dir(_CACHE_NAME)


def cache_key(sha256_hex: str, *, schema_ver: int = SCHEMA_VER) -> str:
    return f"vision:{schema_ver}:{sha256_hex}"


def _path_for(sha256_hex: str, *, schema_ver: int = SCHEMA_VER) -> Path:
    return vision_cache_dir() / f"{schema_ver}_{sha256_hex}.txt"


def get_cached(sha256_hex: str, *, schema_ver: int = SCHEMA_VER) -> str | None:
    path = _path_for(sha256_hex, schema_ver=schema_ver)
    try:
        if not path.is_file():
            return None
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    return text if text.strip() else None


def put_cached(sha256_hex: str, text: str, *, schema_ver: int = SCHEMA_VER) -> None:
    path = _path_for(sha256_hex, schema_ver=schema_ver)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
