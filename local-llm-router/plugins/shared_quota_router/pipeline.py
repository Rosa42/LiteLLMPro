"""Request-enhancement pipeline (envelope + ordered stages).

Runs after quota select on the async hang-point. F1 stages are empty:
vision and memory are registered in later tasks. Closing
``GATEWAY_ENHANCE_ENABLED`` is equivalent to not deploying this module.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Mapping, Protocol

from shared_quota_router.feature_flags import is_gateway_enhance_enabled
from shared_quota_router.models import ApiProtocol
from shared_quota_router.protocol_context import get_metadata_value


def _truthy(value: Any) -> bool:
    if value is True or value == 1:
        return True
    if isinstance(value, str) and value.strip().lower() in {"1", "true", "yes", "on"}:
        return True
    return False


def is_internal_call(request_kwargs: Mapping[str, Any] | None) -> bool:
    """True when this request is a nested vision/memory-extract subcall."""
    if not isinstance(request_kwargs, dict):
        return False
    if _truthy(request_kwargs.get("internal_call")):
        return True
    return _truthy(get_metadata_value(request_kwargs, "internal_call"))


@dataclass(slots=True)
class EnhanceEnvelope:
    """Shared state for enhance stages. ``messages`` is mutated in place."""

    model_group: str
    protocol: ApiProtocol | None
    streaming: bool
    messages: list[Any]
    workspace: str | None
    visual_evidence: list[str]
    memory_hits: list[str]
    internal_call: bool
    parent_request_id: str
    parent_quota_group_id: str
    stage_ms: dict[str, float]
    headers: Mapping[str, Any] | None = None
    translator: Any = None
    select_deployment: Any = None
    release_lease: Any = None
    http_post: Any = None


class Stage(Protocol):
    name: str

    def enabled(self) -> bool: ...

    async def run(self, env: EnhanceEnvelope) -> None: ...


def declared_stages() -> list[Stage]:
    """Linear V1 order: vision then memory_retrieve."""
    from shared_quota_router.memory_retrieve import MemoryRetrieveStage
    from shared_quota_router.vision_compose import VisionComposeStage

    return [VisionComposeStage(), MemoryRetrieveStage()]


async def run_pipeline(env: EnhanceEnvelope) -> None:
    if not is_gateway_enhance_enabled() or env.internal_call:
        return
    for stage in declared_stages():
        if not stage.enabled():
            continue
        started = time.perf_counter()
        await stage.run(env)
        env.stage_ms[stage.name] = (time.perf_counter() - started) * 1000.0
