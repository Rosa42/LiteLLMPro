"""JSONL memory store, one file per normalized workspace."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any


def memory_dir() -> Path:
    raw = (os.environ.get("GATEWAY_MEMORY_DIR") or "").strip()
    if raw:
        return Path(raw)
    from shared_quota_router.data_paths import default_data_dir

    return default_data_dir("gateway-memory")


def memory_file_for(workspace: str) -> Path:
    digest = hashlib.sha256(workspace.encode("utf-8")).hexdigest()[:32]
    return memory_dir() / f"{digest}.jsonl"


def load_entries(workspace: str) -> list[dict[str, Any]]:
    path = memory_file_for(workspace)
    if not path.is_file():
        return []
    out: list[dict[str, Any]] = []
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict) or not str(obj.get("text") or "").strip():
            continue
        if not obj.get("kind"):
            obj["kind"] = "note"
        out.append(obj)
    return out


def append_entry(workspace: str, entry: dict[str, Any]) -> None:
    path = memory_file_for(workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
