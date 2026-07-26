"""Cross-protocol conversion package (post-MVP C1+)."""

from shared_quota_router.conversion.contracts import (
    DIRECTION_CHAT_TO_MESSAGES,
    DIRECTION_MESSAGES_TO_CHAT,
    ConvertedRequest,
    ConvertedResponse,
    feature_fidelity,
    validate_request_against_fidelity,
)

__all__ = [
    "DIRECTION_CHAT_TO_MESSAGES",
    "DIRECTION_MESSAGES_TO_CHAT",
    "ConvertedRequest",
    "ConvertedResponse",
    "feature_fidelity",
    "validate_request_against_fidelity",
]
