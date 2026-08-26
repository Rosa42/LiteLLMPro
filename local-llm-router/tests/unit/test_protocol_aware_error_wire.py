"""ProtocolAwareRoutingError wire fields used by LiteLLM exception mapping."""

from shared_quota_router.models import ApiProtocol
from shared_quota_router.protocol_errors import (
    ProtocolAwareRoutingError,
    ProtocolRoutingReason,
)


def test_message_property_is_settable() -> None:
    err = ProtocolAwareRoutingError(
        "original",
        reason=ProtocolRoutingReason.FEATURE_UNSUPPORTED,
        protocol=ApiProtocol.ANTHROPIC_MESSAGES,
        details={"vision": "upstream"},
    )
    err.message = "rewritten by litellm"
    assert err.message == "rewritten by litellm"
    body = err.to_anthropic_error()
    assert body["error"]["shared_quota"]["details"]["vision"] == "upstream"
