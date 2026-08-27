"""Explicit ordered registry. generic is fallback-only and is not listed here."""

from __future__ import annotations

from shared_quota_router.vision_agents.generic import GenericPreset
from shared_quota_router.vision_agents.opencode import OpenCodePreset
from shared_quota_router.vision_agents.types import AgentPreset

GENERIC = GenericPreset()
OPENCODE = OpenCodePreset()


def matching_presets() -> tuple[AgentPreset, ...]:
    return (OPENCODE,)


def generic_preset() -> AgentPreset:
    return GENERIC


def preset_by_id(agent_id: str) -> AgentPreset | None:
    if agent_id == GENERIC.id:
        return GENERIC
    for preset in matching_presets():
        if preset.id == agent_id:
            return preset
    return None
