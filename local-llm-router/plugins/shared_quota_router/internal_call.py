"""Nested routing helpers for vision / memory-extract subcalls."""

from __future__ import annotations

from shared_quota_router.models import ApiProtocol
from shared_quota_router.protocol_errors import (
    ProtocolAwareRoutingError,
    ProtocolRoutingReason,
)


def child_request_id(parent: str, kind: str, token: str) -> str:
    """Build a child litellm_call_id. ``token`` is typically sha256[:8]."""
    slug = (token or "00000000")[:8]
    return f"{parent}#{kind}:{slug}"


def assert_quota_exclusive(
    parent_quota_group_id: str,
    child_quota_group_id: str,
    *,
    protocol: ApiProtocol | None = None,
    model_group: str | None = None,
) -> None:
    """Fail-closed when a subcall would share the parent execution quota group."""
    parent = (parent_quota_group_id or "").strip()
    child = (child_quota_group_id or "").strip()
    if not parent or not child:
        raise ProtocolAwareRoutingError(
            "internal call missing parent or child quota_group_id",
            reason=ProtocolRoutingReason.CONFIGURATION_INVALID,
            protocol=protocol,
            model_group=model_group,
            details={"internal_call": "quota_missing"},
        )
    if parent == child:
        raise ProtocolAwareRoutingError(
            "internal call quota_group_id must not equal parent execution group",
            reason=ProtocolRoutingReason.CONFIGURATION_INVALID,
            protocol=protocol,
            model_group=model_group,
            details={"internal_call": "quota_overlap", "quota_group_id": child},
        )
