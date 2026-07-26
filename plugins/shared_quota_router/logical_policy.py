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

_DEFAULT_PLANS = Path(__file__).resolve().parents[2] / "config" / "plans.yaml"


def load_logical_models_from_plans(
    path: str | Path | None = None,
) -> dict[str, LogicalModelProtocols]:
    """Load logical_models policy from plans.yaml (source of truth)."""
    p = Path(path) if path else Path(
        os.environ.get("SHARED_QUOTA_PLANS_PATH") or _DEFAULT_PLANS
    )
    if not p.is_file():
        logger.warning("plans file missing for logical_models: %s", p)
        return {}
    try:
        doc = load_plans_file(p)
    except (ConfigValidationError, OSError, ValueError) as exc:
        logger.warning("failed to load logical_models from %s: %s", p, exc)
        return {}
    return dict(doc.logical_models)


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
    p = Path(path) if path else Path(
        os.environ.get("SHARED_QUOTA_LITELLM_YAML")
        or (_DEFAULT_PLANS.parent / "litellm.yaml")
    )
    if not p.is_file():
        return {}
    try:
        import yaml
    except ImportError:
        return {}
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except (OSError, ValueError) as exc:
        logger.warning("failed to read litellm.yaml logical policy: %s", exc)
        return {}
    if not isinstance(data, dict):
        return {}
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
