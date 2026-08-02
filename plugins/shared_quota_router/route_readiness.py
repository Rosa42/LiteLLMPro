"""Per-route readiness for protocol transforms (Responses unified / G0-Native).

禁止把多方向 readiness 折叠成单一全局布尔；调用方应使用
``route_candidate_enabled(route)``。
"""

from __future__ import annotations

import os

from shared_quota_router.feature_flags import (
    is_protocol_aware_gateway_enabled,
    is_protocol_conversion_enabled,
)
from shared_quota_router.models import ApiProtocol, RouteCandidate, TransformOwner


def env_profile() -> str:
    raw = (os.environ.get("SHARED_QUOTA_ENV_PROFILE") or "production").strip().lower()
    return raw or "production"


def is_non_production_profile() -> bool:
    return env_profile() in {"staging", "internal", "dev", "test"}


def is_production_direction_approved(
    source: ApiProtocol,
    target: ApiProtocol,
    owner: TransformOwner,
) -> bool:
    """生产方向显式批准（逗号分隔列表）。

    例: SHARED_QUOTA_PRODUCTION_APPROVED_DIRECTIONS=
      openai_responses>openai_chat:litellm_native
    """
    raw = os.environ.get("SHARED_QUOTA_PRODUCTION_APPROVED_DIRECTIONS") or ""
    token = f"{source.value}>{target.value}:{owner.value}"
    approved = {p.strip() for p in raw.split(",") if p.strip()}
    return token in approved


def readiness(
    source: ApiProtocol,
    target: ApiProtocol,
    owner: TransformOwner,
) -> bool:
    """Whether this (source, target, owner) transform path is implemented/ready."""
    if owner is TransformOwner.DIRECT:
        return source is target
    if owner is TransformOwner.PROJECT_ADAPTER:
        # 本期：PROJECT_ADAPTER Messages→Chat 恒 False（P0-G0A；G0-A/G0-B out of scope）
        # Responses project adapters 亦未排期
        return False
    if owner is TransformOwner.LITELLM_NATIVE:
        if source is ApiProtocol.OPENAI_RESPONSES and target in (
            ApiProtocol.OPENAI_CHAT,
            ApiProtocol.ANTHROPIC_MESSAGES,
        ):
            return True
        # Messages public → Chat via LiteLLM switch (G0-Native)
        if (
            source is ApiProtocol.ANTHROPIC_MESSAGES
            and target is ApiProtocol.OPENAI_CHAT
        ):
            from shared_quota_router.feature_flags import (
                is_native_messages_chat_path_active,
            )

            return is_native_messages_chat_path_active()
        return False
    return False


def route_candidate_enabled(route: RouteCandidate) -> bool:
    """Candidate-level enablement — do not replace with a global conversion flag."""
    if not is_protocol_aware_gateway_enabled():
        return False

    dep = route.deployment
    if route.transform_owner is TransformOwner.DIRECT or route.route_mode.value == "direct":
        return dep.enabled

    if not is_protocol_conversion_enabled():
        return False

    if route.conversion is None:
        return False

    source, target = route.conversion.source, route.conversion.target
    owner = route.transform_owner
    if not readiness(source, target, owner):
        return False

    # Responses native bridge：非生产自由；生产需方向显式批准（Policy A）
    if (
        owner is TransformOwner.LITELLM_NATIVE
        and source is ApiProtocol.OPENAI_RESPONSES
    ):
        if is_non_production_profile():
            return True
        return is_production_direction_approved(source, target, owner)

    # 其它已 readiness 的方向（Messages→Chat 仅 LITELLM_NATIVE）
    return True


def litellm_native_responses_to_chat_enabled() -> bool:
    """Convenience for gates: Responses→Chat native under profile rules."""
    if not is_protocol_aware_gateway_enabled() or not is_protocol_conversion_enabled():
        return False
    source = ApiProtocol.OPENAI_RESPONSES
    target = ApiProtocol.OPENAI_CHAT
    owner = TransformOwner.LITELLM_NATIVE
    if not readiness(source, target, owner):
        return False
    if is_non_production_profile():
        return True
    return is_production_direction_approved(source, target, owner)
