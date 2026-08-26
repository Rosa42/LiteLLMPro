"""Host vs Docker data directories. Not Redis ``sq:*``."""

from __future__ import annotations

from pathlib import Path


def default_data_dir(name: str, *, here: Path | None = None) -> Path:
    """``data/<name>`` next to the plugin install, not the filesystem root.

    Host: ``local-llm-router/plugins/shared_quota_router`` → ``local-llm-router/data``.
    Docker: ``/app/shared_quota_router`` → ``/app/data``.
    """
    package_dir = here if here is not None else Path(__file__).resolve().parent
    if package_dir.parent.name == "plugins":
        return package_dir.parents[1] / "data" / name
    return package_dir.parent / "data" / name
