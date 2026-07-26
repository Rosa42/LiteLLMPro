"""Resolve direct vs convert routes for a deployment (C1-04)."""

from __future__ import annotations

from shared_quota_router.conversion.contracts import validate_request_against_fidelity
from shared_quota_router.models import (
    ApiProtocol,
    ConversionCapability,
    Deployment,
    Feature,
    LogicalModelProtocols,
    RouteCandidate,
    RouteMode,
)
from shared_quota_router.protocol_errors import ProtocolAwareRoutingError


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
    Convert only when flag + logical allowlist + deployment.conversions match.
    """
    if not deployment.enabled:
        return None

    if deployment.supports_protocol(public_protocol):
        needed = set(required_features)
        if stream:
            needed.add(Feature.STREAMING)
        if not all(deployment.supports_feature(f) for f in needed):
            return None
        return RouteCandidate(
            deployment=deployment, route_mode=RouteMode.DIRECT, conversion=None
        )

    if not conversion_enabled or logical is None or not logical.allow_conversion:
        return None
    if deployment.upstream_protocol is None:
        return None

    matching: ConversionCapability | None = None
    for cap in deployment.conversions:
        if cap.source is not public_protocol:
            continue
        if cap.target is not deployment.upstream_protocol:
            continue
        if not logical.allows_conversion_direction(cap.source, cap.target):
            continue
        if stream and not cap.streaming:
            continue
        non_stream = frozenset(
            f for f in required_features if f is not Feature.STREAMING
        )
        if stream:
            # streaming already gated by cap.streaming
            pass
        if not cap.supports_request_features(non_stream or frozenset({Feature.TEXT})):
            # If caller only asked streaming somehow, still require TEXT baseline
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
                stream=stream,
            )
        except ProtocolAwareRoutingError:
            continue
        matching = cap
        break

    if matching is None:
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
    )
