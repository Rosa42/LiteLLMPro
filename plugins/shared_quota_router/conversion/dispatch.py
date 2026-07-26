"""Select converter by direction and apply conversion (C2)."""

from __future__ import annotations

from typing import Any

from shared_quota_router.conversion.adapters.messages_to_chat import (
    MessagesToChatConverter,
)
from shared_quota_router.conversion.contracts import (
    DIRECTION_MESSAGES_TO_CHAT,
    ConvertedRequest,
    ConvertedResponse,
    Direction,
)
from shared_quota_router.feature_flags import is_conversion_routing_active
from shared_quota_router.models import ApiProtocol
from shared_quota_router.protocol_errors import (
    ProtocolAwareRoutingError,
    ProtocolRoutingReason,
)
from shared_quota_router.protocol_observability import record_conversion_result

ROUTE_MODE_META_KEY = "shared_quota_route_mode"
CONVERSION_DIR_META_KEY = "shared_quota_conversion"

_CONVERTERS = {
    DIRECTION_MESSAGES_TO_CHAT: MessagesToChatConverter(),
}


def direction_key(source: ApiProtocol, target: ApiProtocol) -> str:
    return f"{source.value}>{target.value}"


def parse_direction_key(raw: str | None) -> Direction | None:
    if not raw or ">" not in raw:
        return None
    left, right = raw.split(">", 1)
    try:
        return (ApiProtocol(left.strip()), ApiProtocol(right.strip()))
    except ValueError:
        return None


def get_converter(direction: Direction):
    conv = _CONVERTERS.get(direction)
    if conv is None:
        raise ProtocolAwareRoutingError(
            f"no converter registered for {direction[0].value}->{direction[1].value}",
            reason=ProtocolRoutingReason.NO_COMPATIBLE_DEPLOYMENT,
            protocol=direction[0],
        )
    return conv


def convert_public_request(
    public_payload: dict[str, Any],
    *,
    direction: Direction,
) -> ConvertedRequest:
    if not is_conversion_routing_active():
        raise ProtocolAwareRoutingError(
            "protocol conversion is disabled",
            reason=ProtocolRoutingReason.UNSUPPORTED_PUBLIC_PROTOCOL,
            protocol=direction[0],
        )
    converter = get_converter(direction)
    try:
        out = converter.convert_request(public_payload)
        if out.dropped_fields:
            raise ProtocolAwareRoutingError(
                f"conversion would drop fields: {out.dropped_fields}",
                reason=ProtocolRoutingReason.FEATURE_UNSUPPORTED,
                protocol=direction[0],
                details={"dropped_fields": list(out.dropped_fields)},
            )
        record_conversion_result(
            direction=direction_key(*direction), result="request_ok"
        )
        return out
    except ProtocolAwareRoutingError as exc:
        record_conversion_result(
            direction=direction_key(*direction),
            result="failure",
            reason=exc.reason.value,
        )
        raise


def convert_upstream_response(
    upstream_payload: dict[str, Any],
    *,
    direction: Direction,
) -> ConvertedResponse:
    converter = get_converter(direction)
    try:
        out = converter.convert_response(upstream_payload)
        if out.dropped_fields:
            raise ProtocolAwareRoutingError(
                f"response conversion would drop fields: {out.dropped_fields}",
                reason=ProtocolRoutingReason.FEATURE_UNSUPPORTED,
                protocol=direction[0],
            )
        record_conversion_result(
            direction=direction_key(*direction), result="success"
        )
        return out
    except ProtocolAwareRoutingError as exc:
        record_conversion_result(
            direction=direction_key(*direction),
            result="failure",
            reason=exc.reason.value,
        )
        raise


def convert_upstream_error(
    upstream_error: dict[str, Any],
    *,
    direction: Direction,
) -> dict[str, Any]:
    converter = get_converter(direction)
    return converter.convert_error(upstream_error)
