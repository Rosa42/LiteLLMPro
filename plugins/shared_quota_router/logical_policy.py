"""Load logical-model policy for runtime routing (P1-01 / P1-02).

Source order:
1. Explicit constructor / bootstrap injection
2. ``SHARED_QUOTA_PLANS_PATH`` or default ``config/plans.yaml``
3. Generated ``shared_quota_logical_models`` section in litellm.yaml
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Mapping

from shared_quota_router.config_schema import (
    ConfigValidationError,
    _parse_logical_models,
    load_plans_file,
)
from shared_quota_router.models import LogicalModelProtocols

logger = logging.getLogger(__name__)

# 仓库布局：plugins/shared_quota_router → <repo>/config/plans.yaml
# Docker 布局：代码在 /app/shared_quota_router，配置挂载为 /app/config.yaml
_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_PLANS = _REPO_ROOT / "config" / "plans.yaml"


def _candidate_plans_paths() -> list[Path]:
    paths: list[Path] = []
    env = os.environ.get("SHARED_QUOTA_PLANS_PATH")
    if env:
        paths.append(Path(env))
    paths.extend(
        [
            _DEFAULT_PLANS,
            Path("/app/config/plans.yaml"),
        ]
    )
    # 去重且保序
    seen: set[str] = set()
    out: list[Path] = []
    for p in paths:
        key = str(p)
        if key in seen:
            continue
        seen.add(key)
        out.append(p)
    return out


def _candidate_litellm_yaml_paths() -> list[Path]:
    paths: list[Path] = []
    for key in ("SHARED_QUOTA_LITELLM_YAML", "LITELLM_CONFIG"):
        env = os.environ.get(key)
        if env:
            paths.append(Path(env))
    paths.extend(
        [
            _DEFAULT_PLANS.parent / "litellm.yaml",
            Path("/app/config.yaml"),  # compose 挂载点
            Path("/app/config/litellm.yaml"),
        ]
    )
    seen: set[str] = set()
    out: list[Path] = []
    for p in paths:
        key = str(p)
        if key in seen:
            continue
        seen.add(key)
        out.append(p)
    return out


def load_logical_models_from_plans(
    path: str | Path | None = None,
) -> dict[str, LogicalModelProtocols]:
    """Load logical_models policy from plans.yaml (source of truth)."""
    candidates = [Path(path)] if path else _candidate_plans_paths()
    last_missing: Path | None = None
    for p in candidates:
        if not p.is_file():
            last_missing = p
            continue
        try:
            doc = load_plans_file(p)
        except (ConfigValidationError, OSError, ValueError) as exc:
            logger.warning("failed to load logical_models from %s: %s", p, exc)
            return {}
        return dict(doc.logical_models)
    if last_missing is not None:
        logger.warning("plans file missing for logical_models: %s", last_missing)
    return {}


def parse_logical_models_section(
    raw: Mapping[str, Any] | None,
) -> dict[str, LogicalModelProtocols]:
    """Parse generated ``shared_quota_logical_models`` mapping."""
    if not raw:
        return {}
    try:
        return _parse_logical_models(raw)
    except ConfigValidationError as exc:
        logger.warning("invalid shared_quota_logical_models: %s", exc)
        return {}


def load_logical_models_from_litellm_yaml(
    path: str | Path | None = None,
) -> dict[str, LogicalModelProtocols]:
    """Parse ``shared_quota_logical_models`` from a generated litellm.yaml."""
    try:
        import yaml
    except ImportError:
        return {}

    candidates = [Path(path)] if path else _candidate_litellm_yaml_paths()
    for p in candidates:
        if not p.is_file():
            continue
        try:
            data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        except (OSError, ValueError) as exc:
            logger.warning("failed to read litellm.yaml logical policy: %s", exc)
            continue
        if not isinstance(data, dict):
            continue
        section = data.get("shared_quota_logical_models")
        if isinstance(section, dict):
            return parse_logical_models_section(section)
    return {}


def resolve_runtime_logical_models(
    *,
    explicit: dict[str, LogicalModelProtocols] | None = None,
) -> dict[str, LogicalModelProtocols]:
    """Prefer explicit map, then plans.yaml, then generated litellm.yaml."""
    if explicit:
        return dict(explicit)
    from_plans = load_logical_models_from_plans()
    if from_plans:
        return from_plans
    return load_logical_models_from_litellm_yaml()
