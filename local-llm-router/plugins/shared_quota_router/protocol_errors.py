"""Routing / protocol-aware selection errors (M2-05).

Pre-call capability / configuration failures must NOT be classified as
provider outage or quota exhaustion, and must not mutate circuit state.
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from shared_quota_router.models import ApiProtocol


class NoAvailableDeploymentError(Exception):
    """Raised when no deployment can be selected (fail-closed or empty candidates)."""


class ProtocolRoutingReason(str, Enum):
    MISSING_PROTOCOL = "missing_protocol"
    UNSUPPORTED_PUBLIC_PROTOCOL = "unsupported_public_protocol"
    NO_COMPATIBLE_DEPLOYMENT = "no_compatible_deployment"
    CONFIGURATION_INVALID = "configuration_invalid"
    FEATURE_UNSUPPORTED = "feature_unsupported"


class ProtocolAwareRoutingError(NoAvailableDeploymentError):
    """Raised when selection fails for protocol/capability reasons (pre-lease)."""

    def __init__(
        self,
        message: str,
        *,
        reason: ProtocolRoutingReason,
        protocol: ApiProtocol | None = None,
        model_group: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.reason = reason
        self.protocol = protocol
        self.model_group = model_group
        self.details = details or {}
        # 供 LiteLLM anthropic_endpoints getattr(e, "status_code"|"type"|"message")
        # 剥成 ProxyException 时保留 HTTP 400 + invalid_request_error（P1-A5）
        self._wire_message = str(message)

    @property
    def message(self) -> str:
        return self._wire_message

    @property
    def status_code(self) -> int:
        """协议门控失败固定 400（禁止落成 500）。"""
        return 400

    @property
    def type(self) -> str:
        """Anthropic / ProxyException 的 error.type。"""
        if self.reason is ProtocolRoutingReason.NO_COMPATIBLE_DEPLOYMENT:
            return "api_error"
        return "invalid_request_error"

    def to_public_error(self) -> dict[str, Any]:
        """Native structured error for the requested public protocol."""
        if self.protocol is ApiProtocol.ANTHROPIC_MESSAGES:
            return self.to_anthropic_error()
        return self.to_openai_error()

    def to_openai_error(self) -> dict[str, Any]:
        code = _openai_code(self.reason)
        err_type = (
            "invalid_request_error"
            if self.reason
            in {
                ProtocolRoutingReason.UNSUPPORTED_PUBLIC_PROTOCOL,
                ProtocolRoutingReason.CONFIGURATION_INVALID,
                ProtocolRoutingReason.MISSING_PROTOCOL,
                ProtocolRoutingReason.FEATURE_UNSUPPORTED,
            }
            else "api_error"
        )
        return {
            "error": {
                "message": str(self),
                "type": err_type,
                "param": None,
                "code": code,
                "shared_quota": {
                    "reason": self.reason.value,
                    "protocol": self.protocol.value if self.protocol else None,
                    "model_group": self.model_group,
                },
            }
        }

    def to_anthropic_error(self) -> dict[str, Any]:
        err_type = (
            "api_error"
            if self.reason is ProtocolRoutingReason.NO_COMPATIBLE_DEPLOYMENT
            else "invalid_request_error"
        )
        return {
            "type": "error",
            "error": {
                "type": err_type,
                "message": str(self),
                "shared_quota": {
                    "reason": self.reason.value,
                    "protocol": self.protocol.value if self.protocol else None,
                    "model_group": self.model_group,
                },
            },
        }


def _openai_code(reason: ProtocolRoutingReason) -> str:
    mapping = {
        ProtocolRoutingReason.MISSING_PROTOCOL: "protocol_required",
        ProtocolRoutingReason.UNSUPPORTED_PUBLIC_PROTOCOL: "protocol_not_enabled",
        ProtocolRoutingReason.NO_COMPATIBLE_DEPLOYMENT: "no_compatible_deployment",
        ProtocolRoutingReason.CONFIGURATION_INVALID: "configuration_invalid",
        ProtocolRoutingReason.FEATURE_UNSUPPORTED: "feature_unsupported",
    }
    return mapping.get(reason, "no_available_deployment")
