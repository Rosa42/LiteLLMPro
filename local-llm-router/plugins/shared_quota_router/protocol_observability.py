"""Protocol routing observability helpers (M4-01).

Records public/upstream protocol, route mode, result, and failure reason.
Never logs prompts, responses, credentials, or Authorization headers.
Conversion metrics stay dormant in MVP (names reserved, always zero).
"""

from __future__ import annotations

import hashlib
import logging
import re

from shared_quota_router.feature_flags import (
    is_protocol_aware_gateway_enabled,
    metrics_label_salt,
    metrics_raw_labels_allowed,
)
from shared_quota_router.metrics import get_counter, inc
from shared_quota_router.models import ApiProtocol

logger = logging.getLogger(__name__)

# Reserved / dormant in MVP — do not increment on conversion paths (none yet)
CONVERSION_METRIC_NAMES = (
    "shared_quota_protocol_conversion_total",
    "shared_quota_protocol_conversion_failure_total",
)

_SECRET_PATTERNS = re.compile(
    r"(?i)(api[_-]?key|authorization|bearer\s+[a-z0-9\-._~+/]+=*|sk-[a-z0-9]+|"
    r"password\s*[:=]|prompt\s*[:=])"
)


def sanitize_operational_label(value: str | None, *, kind: str = "id") -> str:
    """Hash or suppress sensitive operational labels for multi-tenant metrics."""
    if value is None or value == "":
        return "none"
    text = str(value)
    salt = metrics_label_salt()
    if salt:
        digest = hashlib.sha256(f"{salt}:{kind}:{text}".encode()).hexdigest()[:12]
        return f"h_{digest}"
    if metrics_raw_labels_allowed():
        return text[:64]
    return "redacted"


def record_route_selection(
    *,
    public_protocol: ApiProtocol | str | None,
    upstream_protocol: ApiProtocol | str | None = None,
    route_mode: str,
    result: str,
    model_group: str | None = None,
    deployment_id: str | None = None,
    quota_group_id: str | None = None,
) -> None:
    """Increment selection/rejection counters with safe labels."""
    pub = _proto_str(public_protocol)
    up = _proto_str(upstream_protocol) or pub
    labels = {
        "public_protocol": pub,
        "upstream_protocol": up,
        "route_mode": route_mode,
        "result": result,
        "model_group": sanitize_operational_label(model_group, kind="model"),
        "deployment_id": sanitize_operational_label(deployment_id, kind="dep"),
        "quota_group_id": sanitize_operational_label(quota_group_id, kind="qg"),
        "gateway": "on" if is_protocol_aware_gateway_enabled() else "off",
    }
    inc("shared_quota_protocol_route_total", **labels)
    logger.info(
        "protocol_route public=%s upstream=%s mode=%s result=%s model=%s dep=%s gateway=%s",
        pub,
        up,
        route_mode,
        result,
        labels["model_group"],
        labels["deployment_id"],
        labels["gateway"],
    )


def record_protocol_rejection(
    *,
    public_protocol: ApiProtocol | str | None,
    reason: str,
    model_group: str | None = None,
    route_mode: str = "direct",
) -> None:
    pub = _proto_str(public_protocol)
    inc(
        "shared_quota_protocol_reject_total",
        public_protocol=pub,
        reason=str(reason)[:64],
        route_mode=route_mode,
        model_group=sanitize_operational_label(model_group, kind="model"),
        gateway="on" if is_protocol_aware_gateway_enabled() else "off",
    )
    # Keep legacy counter used by M2/M3 callback path
    inc(
        "shared_quota_protocol_no_route_total",
        reason=str(reason)[:64],
        protocol=pub,
    )
    logger.info(
        "protocol_reject protocol=%s reason=%s model=%s",
        pub,
        reason,
        sanitize_operational_label(model_group, kind="model"),
    )


def record_conversion_result(
    *,
    direction: str,
    result: str,
    reason: str | None = None,
) -> None:
    """Increment conversion counters (C2). Never include prompt/body labels."""
    from shared_quota_router.feature_flags import is_conversion_routing_active

    safe_direction = str(direction)[:96]
    safe_result = str(result)[:32]
    inc(
        "shared_quota_protocol_conversion_total",
        direction=safe_direction,
        result=safe_result,
        conversion="on" if is_conversion_routing_active() else "off",
    )
    if safe_result == "failure":
        inc(
            "shared_quota_protocol_conversion_failure_total",
            direction=safe_direction,
            reason=(reason or "unknown")[:64],
        )
    logger.info(
        "protocol_conversion direction=%s result=%s reason=%s",
        safe_direction,
        safe_result,
        (reason or "-")[:64],
    )


def conversion_metrics_dormant() -> bool:
    """True when conversion counters are still zero (no live convert traffic)."""
    return all(get_counter(name) == 0.0 for name in CONVERSION_METRIC_NAMES)


def log_text_has_secrets(text: str) -> bool:
    return bool(_SECRET_PATTERNS.search(text))


def _proto_str(value: ApiProtocol | str | None) -> str:
    if value is None:
        return "none"
    if isinstance(value, ApiProtocol):
        return value.value
    return str(value)
