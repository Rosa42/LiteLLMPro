"""Resolve which vision preset to use. Prefer miss over mis-detect."""

from __future__ import annotations

import logging
from typing import Any, Mapping

from shared_quota_router.feature_flags import is_vision_agent_fingerprints_enabled
from shared_quota_router.vision_agents.registry import (
    generic_preset,
    matching_presets,
    preset_by_id,
)
from shared_quota_router.vision_agents.types import AgentPreset

logger = logging.getLogger(__name__)

MAX_ADDENDUM_CHARS = 1000


def _header(headers: Mapping[str, Any] | None, name: str) -> str:
    if not isinstance(headers, Mapping):
        return ""
    want = name.lower()
    for key, value in headers.items():
        if str(key).lower() == want:
            return str(value or "").strip()
    return ""


def resolve_preset(
    headers: Mapping[str, Any] | None,
    messages: list[Any] | None,
) -> tuple[AgentPreset, str]:
    client = _header(headers, "x-agent-client").lower()
    if client == "generic":
        return generic_preset(), "header_force"
    if client:
        found = preset_by_id(client)
        if found is not None and found.id != "generic":
            return found, "header"
    for preset in matching_presets():
        if preset.match_header(headers or {}):
            return preset, "ua"
    if is_vision_agent_fingerprints_enabled() and isinstance(messages, list):
        for preset in matching_presets():
            if preset.match_messages(messages):
                return preset, "fingerprint"
    return generic_preset(), "fallback"


def clamp_addendum(preset: AgentPreset, match: str) -> tuple[AgentPreset, str, str]:
    """Drop an oversized / crashing addendum by falling back to generic."""
    try:
        addendum = (preset.system_addendum() or "").strip()
    except Exception as exc:  # noqa: BLE001 — preset bugs must not fail-close vision
        logger.warning(
            "enhance_vision addendum_error agent=%s type=%s",
            preset.id,
            type(exc).__name__,
        )
        return generic_preset(), "extract_error", ""
    if len(addendum) > MAX_ADDENDUM_CHARS:
        logger.warning("enhance_vision addendum_overflow agent=%s", preset.id)
        return generic_preset(), "extract_error", ""
    return preset, match, addendum
