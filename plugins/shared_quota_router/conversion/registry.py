"""Resolve direct vs convert routes for a deployment (C1-04)."""

from __future__ import annotations

from shared_quota_router.conversion.contracts import validate_request_against_fidelity
from shared_quota_router.models import (
    ApiProtocol,
    ConversionCapability,
    Deployment,
    Feature,
    FidelityClass,
    LogicalModelProtocols,
    RouteCandidate,
    RouteMode,
    TransformOwner,
)
from shared_quota_router.protocol_errors import ProtocolAwareRoutingError
from shared_quota_router.route_readiness import readiness


def _native_responses_bridge_cap(
    *,
    source: ApiProtocol,
    target: ApiProtocol,
    stream: bool,
) -> ConversionCapability:
    """Synthetic capability: LiteLLM owns transform (no project adapter)."""
    return ConversionCapability(
        source=source,
        target=target,
        request_features=frozenset({Feature.TEXT, Feature.STREAMING, Feature.TOOLS}),
        response_features=frozenset({Feature.TEXT, Feature.STREAMING, Feature.TOOLS}),
        streaming=True,
        fidelity=FidelityClass.LOSSY_SAFE,
    )


def resolve_route(
    deployment: Deployment,
    *,
    public_protocol: ApiProtocol,
    required_features: frozenset[Feature],
    stream: bool,
    logical: LogicalModelProtocols | None,
    conversion_enabled: bool,
) -> RouteCandidate | None:
    """Return a route candidate or None if this deployment cannot serve the request.

    Prefers direct when ``upstream_protocol`` matches ``public_protocol``.
    Convert: LiteLLM native bridge for Responses→Chat/Messages when ready;
    else project adapter when deployment.conversions + registry allow.

    C4：入口统一 ``effective_stream = stream ∨ STREAMING∈required``；
    之后只使用 effective_stream，禁止两参数不一致。
    """
    if not deployment.enabled:
        return None

    # P0-C4 / P1-C4-BYPASS：stream 与 required_features 边界归一化
    effective_stream = bool(stream) or (
        Feature.STREAMING in (required_features or frozenset())
    )

    if deployment.supports_protocol(public_protocol):
        needed = set(required_features)
        if effective_stream:
            needed.add(Feature.STREAMING)
        if not all(deployment.supports_feature(f) for f in needed):
            return None
        return RouteCandidate(
            deployment=deployment,
            route_mode=RouteMode.DIRECT,
            conversion=None,
            transform_owner=TransformOwner.DIRECT,
        )

    if not conversion_enabled or logical is None or not logical.allow_conversion:
        return None
    if deployment.upstream_protocol is None:
        return None
    if not logical.allows_conversion_direction(
        public_protocol, deployment.upstream_protocol
    ):
        return None

    # Responses → Chat/Messages：优先 LiteLLM 原生 bridge（无项目 adapter）
    if public_protocol is ApiProtocol.OPENAI_RESPONSES and readiness(
        public_protocol,
        deployment.upstream_protocol,
        TransformOwner.LITELLM_NATIVE,
    ):
        if deployment.upstream_protocol not in (
            ApiProtocol.OPENAI_CHAT,
            ApiProtocol.ANTHROPIC_MESSAGES,
        ):
            return None
        needed = set(required_features) or {Feature.TEXT}
        if effective_stream:
            needed.add(Feature.STREAMING)
        if not deployment.supports_feature(Feature.TEXT):
            return None
        extras = needed - {Feature.TEXT, Feature.STREAMING}
        if extras:
            return None
        cap = _native_responses_bridge_cap(
            source=public_protocol,
            target=deployment.upstream_protocol,
            stream=effective_stream,
        )
        if effective_stream and not cap.streaming:
            return None
        return RouteCandidate(
            deployment=deployment,
            route_mode=RouteMode.CONVERT,
            conversion=cap,
            transform_owner=TransformOwner.LITELLM_NATIVE,
        )

    # Messages → Chat：G0-Native；C4 硬禁流（streaming=False；effective_stream → None）
    if (
        public_protocol is ApiProtocol.ANTHROPIC_MESSAGES
        and deployment.upstream_protocol is ApiProtocol.OPENAI_CHAT
        and readiness(
            public_protocol,
            deployment.upstream_protocol,
            TransformOwner.LITELLM_NATIVE,
        )
    ):
        if effective_stream:
            return None
        needed = set(required_features) or {Feature.TEXT}
        if not deployment.supports_feature(Feature.TEXT):
            return None
        extras = needed - {Feature.TEXT}
        if extras:
            return None
        cap = ConversionCapability(
            source=public_protocol,
            target=deployment.upstream_protocol,
            request_features=frozenset({Feature.TEXT}),
            response_features=frozenset({Feature.TEXT}),
            streaming=False,
            fidelity=FidelityClass.LOSSY_SAFE,
        )
        return RouteCandidate(
            deployment=deployment,
            route_mode=RouteMode.CONVERT,
            conversion=cap,
            transform_owner=TransformOwner.LITELLM_NATIVE,
        )

    matching: ConversionCapability | None = None
    for cap in deployment.conversions:
        if cap.source is not public_protocol:
            continue
        if cap.target is not deployment.upstream_protocol:
            continue
        if not logical.allows_conversion_direction(cap.source, cap.target):
            continue
        if effective_stream and not cap.streaming:
            continue
        non_stream = frozenset(
            f for f in required_features if f is not Feature.STREAMING
        )
        if not cap.supports_request_features(non_stream or frozenset({Feature.TEXT})):
            check = non_stream if non_stream else frozenset({Feature.TEXT})
            if not cap.supports_request_features(check):
                continue
        try:
            validate_request_against_fidelity(
                source=cap.source,
                target=cap.target,
                required_features=required_features
                if required_features
                else frozenset({Feature.TEXT}),
                stream=effective_stream,
            )
        except ProtocolAwareRoutingError:
            continue
        matching = cap
        break

    if matching is None:
        return None

    # 本期 PROJECT_ADAPTER（含 Messages→Chat）readiness 恒 False；禁止 ADAPTER 回退
    if not readiness(
        matching.source,
        matching.target,
        TransformOwner.PROJECT_ADAPTER,
    ):
        return None

    upstream_needed = frozenset(
        f for f in matching.request_features if f is not Feature.STREAMING
    )
    if not all(deployment.supports_feature(f) for f in upstream_needed):
        return None

    return RouteCandidate(
        deployment=deployment,
        route_mode=RouteMode.CONVERT,
        conversion=matching,
        transform_owner=TransformOwner.PROJECT_ADAPTER,
    )
