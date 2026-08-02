"""Directional conversion contracts (C1).

source = public protocol; target = upstream protocol.
Initial matrix is conservative: text-only non-streaming for pilot directions.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from shared_quota_router.models import ApiProtocol, Feature, FidelityClass
from shared_quota_router.protocol_errors import (
    ProtocolAwareRoutingError,
    ProtocolRoutingReason,
)

Direction = tuple[ApiProtocol, ApiProtocol]

DIRECTION_MESSAGES_TO_CHAT: Direction = (
    ApiProtocol.ANTHROPIC_MESSAGES,
    ApiProtocol.OPENAI_CHAT,
)
DIRECTION_CHAT_TO_MESSAGES: Direction = (
    ApiProtocol.OPENAI_CHAT,
    ApiProtocol.ANTHROPIC_MESSAGES,
)

# Per-direction feature fidelity. Missing feature ⇒ UNSUPPORTED.
_FIDELITY: dict[Direction, dict[Feature, FidelityClass]] = {
    DIRECTION_MESSAGES_TO_CHAT: {
        Feature.TEXT: FidelityClass.EQUIVALENT,
        Feature.STREAMING: FidelityClass.UNSUPPORTED,  # until C4
        Feature.TOOLS: FidelityClass.UNSUPPORTED,  # C2 pilot
        Feature.REASONING: FidelityClass.LOSSY_UNSAFE,
        Feature.PROMPT_CACHE: FidelityClass.UNSUPPORTED,
        Feature.STRUCTURED_OUTPUT: FidelityClass.UNSUPPORTED,
        Feature.IMAGE: FidelityClass.UNSUPPORTED,
        Feature.PARALLEL_TOOL_CALLS: FidelityClass.UNSUPPORTED,
        Feature.CITATIONS: FidelityClass.UNSUPPORTED,
    },
    DIRECTION_CHAT_TO_MESSAGES: {
        Feature.TEXT: FidelityClass.EQUIVALENT,
        Feature.STREAMING: FidelityClass.UNSUPPORTED,
        Feature.TOOLS: FidelityClass.UNSUPPORTED,
        Feature.REASONING: FidelityClass.LOSSY_UNSAFE,
        Feature.PROMPT_CACHE: FidelityClass.UNSUPPORTED,
        Feature.STRUCTURED_OUTPUT: FidelityClass.UNSUPPORTED,
        Feature.IMAGE: FidelityClass.UNSUPPORTED,
        Feature.PARALLEL_TOOL_CALLS: FidelityClass.UNSUPPORTED,
        Feature.CITATIONS: FidelityClass.UNSUPPORTED,
    },
}

_REJECT_FIDELITIES = frozenset(
    {FidelityClass.LOSSY_UNSAFE, FidelityClass.UNSUPPORTED}
)


def feature_fidelity(direction: Direction, feature: Feature) -> FidelityClass:
    return _FIDELITY.get(direction, {}).get(feature, FidelityClass.UNSUPPORTED)


def validate_request_against_fidelity(
    *,
    source: ApiProtocol,
    target: ApiProtocol,
    required_features: frozenset[Feature],
    stream: bool,
) -> None:
    direction = (source, target)
    if direction not in _FIDELITY:
        raise ProtocolAwareRoutingError(
            f"no conversion contract for {source.value} -> {target.value}",
            reason=ProtocolRoutingReason.NO_COMPATIBLE_DEPLOYMENT,
            protocol=source,
        )
    features = set(required_features)
    if stream:
        features.add(Feature.STREAMING)
    for feat in features:
        klass = feature_fidelity(direction, feat)
        if klass in _REJECT_FIDELITIES:
            raise ProtocolAwareRoutingError(
                f"conversion {source.value}->{target.value} rejects feature "
                f"{feat.value} ({klass.value})",
                reason=ProtocolRoutingReason.FEATURE_UNSUPPORTED,
                protocol=source,
                details={"feature": feat.value, "fidelity": klass.value},
            )


@dataclass(slots=True)
class ConvertedRequest:
    payload: dict[str, Any]
    warnings: list[str] = field(default_factory=list)
    dropped_fields: list[str] = field(default_factory=list)


@dataclass(slots=True)
class ConvertedResponse:
    payload: dict[str, Any]
    warnings: list[str] = field(default_factory=list)
    dropped_fields: list[str] = field(default_factory=list)
