"""C4: streaming conversion remains unsupported until evaluation Go."""

from __future__ import annotations

import pytest

from shared_quota_router.conversion.contracts import (
    DIRECTION_MESSAGES_TO_CHAT,
    feature_fidelity,
    validate_request_against_fidelity,
)
from shared_quota_router.models import ApiProtocol, Feature, FidelityClass
from shared_quota_router.protocol_errors import ProtocolAwareRoutingError


def test_c4_streaming_still_unsupported_on_pilot_matrix() -> None:
    assert (
        feature_fidelity(DIRECTION_MESSAGES_TO_CHAT, Feature.STREAMING)
        is FidelityClass.UNSUPPORTED
    )
    with pytest.raises(ProtocolAwareRoutingError) as ei:
        validate_request_against_fidelity(
            source=ApiProtocol.ANTHROPIC_MESSAGES,
            target=ApiProtocol.OPENAI_CHAT,
            required_features=frozenset({Feature.TEXT}),
            stream=True,
        )
    assert ei.value.reason.value == "feature_unsupported"


@pytest.mark.skip(reason="C4 No-Go: streaming conversion adapter not implemented")
def test_c4_first_converted_visible_event_defines_first_byte() -> None:
    raise AssertionError("implement when streaming conversion is proven")
