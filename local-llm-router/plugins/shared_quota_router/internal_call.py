"""Nested routing helpers for vision / memory-extract subcalls."""

from __future__ import annotations

import logging
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Callable, Iterator

from shared_quota_router.models import ApiProtocol, Feature
from shared_quota_router.protocol_context import FEATURES_META_KEY
from shared_quota_router.protocol_errors import (
    ProtocolAwareRoutingError,
    ProtocolRoutingReason,
)

logger = logging.getLogger(__name__)

_TRUSTED_INTERNAL: ContextVar[bool] = ContextVar("sq_trusted_internal", default=False)


class _UpstreamHttpError(Exception):
    """Carrier so classifier sees a raw HTTP status, not FEATURE_UNSUPPORTED."""

    def __init__(self, status: int, message: str = "") -> None:
        super().__init__(message or f"upstream {status}")
        self.status_code = int(status)


@contextmanager
def trusted_internal() -> Iterator[None]:
    """Mark the current task as a process-trusted nested select."""
    token = _TRUSTED_INTERNAL.set(True)
    try:
        yield
    finally:
        _TRUSTED_INTERNAL.reset(token)


def is_trusted_internal() -> bool:
    return bool(_TRUSTED_INTERNAL.get())


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


def select_internal_deployment(
    model_group: str,
    *,
    select: Callable[..., Any],
    protocol: ApiProtocol = ApiProtocol.ANTHROPIC_MESSAGES,
    required_features: frozenset[Feature] | None = None,
    parent_request_id: str,
    parent_quota_group_id: str,
    child_id: str | None = None,
    messages: list[Any] | None = None,
    kind: str = "vision",
) -> dict[str, Any]:
    """Select a nested deployment, skipping public opt-in via ContextVar only."""
    feats = required_features if required_features is not None else frozenset(
        {Feature.TEXT, Feature.IMAGE}
    )
    feat_values = [f.value for f in sorted(feats, key=lambda item: item.value)]
    rid = child_id or child_request_id(parent_request_id, kind, "00000000")
    meta = {
        "protocol": protocol.value,
        FEATURES_META_KEY: feat_values,
        "internal_kind": kind,
    }
    kwargs: dict[str, Any] = {
        "litellm_call_id": rid,
        "messages": messages or [{"role": "user", "content": "internal"}],
        "litellm_metadata": dict(meta),
        "metadata": dict(meta),
        "_sq_nested_child": True,
    }
    with trusted_internal():
        entry = select(
            model_group,
            messages=kwargs["messages"],
            request_kwargs=kwargs,
        )
    if not isinstance(entry, dict):
        raise ProtocolAwareRoutingError(
            "internal select returned no deployment",
            reason=ProtocolRoutingReason.FEATURE_UNSUPPORTED,
            protocol=protocol,
            model_group=model_group,
            details={"vision": "upstream"},
        )
    info = entry.get("model_info") if isinstance(entry.get("model_info"), dict) else {}
    child_qg = str(info.get("quota_group_id") or "")
    assert_quota_exclusive(
        parent_quota_group_id,
        child_qg,
        protocol=protocol,
        model_group=model_group,
    )
    return entry


def report_internal_outcome(
    kwargs: dict[str, Any],
    *,
    success: bool,
    status_code: int | None = None,
    exception: BaseException | None = None,
    callback: Any = None,
) -> None:
    """Feed a nested HTTP result into SharedQuotaCallback on_success/on_failure."""
    cb = callback
    if cb is None:
        try:
            from shared_quota_router.bootstrap import get_callback

            cb = get_callback()
        except Exception as exc:  # noqa: BLE001
            logger.warning("report_internal_outcome: no callback: %s", exc)
            return
    if cb is None:
        return
    payload = dict(kwargs)
    payload["_sq_nested_child"] = True
    if status_code is not None:
        payload["response_status_code"] = int(status_code)
    try:
        if success:
            cb.on_success(payload)
            return
        exc: BaseException | None = exception
        if isinstance(exc, ProtocolAwareRoutingError):
            exc = None
        if exc is None:
            status = int(status_code) if status_code is not None else 400
            exc = _UpstreamHttpError(status)
        payload["exception"] = exc
        cb.on_failure(payload, exc)
    except Exception as exc:  # noqa: BLE001 — nested accounting must not break parent
        logger.warning("report_internal_outcome failed: %s", exc)
