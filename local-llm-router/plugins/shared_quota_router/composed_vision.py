"""S5: composed-model IMAGE deferral and post-select peel.

Vision translation is not implemented here. Default: fail-closed if a composed
request still has image blocks after select. Stub peel is env-gated for probes.
"""

from __future__ import annotations

import os
from typing import Any

from shared_quota_router.feature_flags import (
    is_gateway_enhance_enabled,
    is_vision_compose_enabled,
)
from shared_quota_router.models import ApiProtocol, Feature, LogicalModelProtocols
from shared_quota_router.protocol_errors import (
    ProtocolAwareRoutingError,
    ProtocolRoutingReason,
)

_IMAGE_TYPES = frozenset({"image", "image_url"})


def composed_model_names() -> frozenset[str]:
    raw = (os.environ.get("S5_COMPOSED_MODELS") or "").strip()
    if not raw:
        return frozenset()
    return frozenset(part.strip() for part in raw.split(",") if part.strip())


def stub_peel_enabled() -> bool:
    raw = (os.environ.get("S5_STUB_PEEL") or "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def defers_image_gate(
    model_group: str | None,
    logical: LogicalModelProtocols | None = None,
) -> bool:
    if not model_group:
        return False
    if model_group in composed_model_names():
        return True
    return logical is not None and logical.compose is not None


def capability_features(
    model_group: str | None,
    features: frozenset[Feature],
    *,
    logical: LogicalModelProtocols | None = None,
) -> frozenset[Feature]:
    """Features used for deployment capability checks.

    Composed facades keep IMAGE in the request context but must not require
    IMAGE on the text execution deployment.
    """
    if Feature.IMAGE in features and defers_image_gate(model_group, logical):
        return features - {Feature.IMAGE}
    return features


def content_has_image(content: Any) -> bool:
    if isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = str(block.get("type") or "")
            if btype in _IMAGE_TYPES:
                return True
            if btype == "tool_result" and content_has_image(block.get("content")):
                return True
    return False


def messages_have_image(messages: Any) -> bool:
    if not isinstance(messages, list):
        return False
    for msg in messages:
        if isinstance(msg, dict) and content_has_image(msg.get("content")):
            return True
    return False


def _replacement_text() -> str:
    marker = (os.environ.get("P0_PROBE_B_MARKER") or "").strip()
    if marker:
        return f"[S5 peeled image. token {marker}]"
    return "[S5 peeled image]"


def _peel_content_list(blocks: list[Any], replacement: str) -> None:
    for i, block in enumerate(blocks):
        if not isinstance(block, dict):
            continue
        btype = str(block.get("type") or "")
        if btype in _IMAGE_TYPES:
            blocks[i] = {"type": "text", "text": replacement}
            continue
        if btype == "tool_result":
            inner = block.get("content")
            if isinstance(inner, list):
                _peel_content_list(inner, replacement)


def peel_messages(messages: Any, replacement: str) -> None:
    if not isinstance(messages, list):
        return
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        if isinstance(content, list):
            _peel_content_list(content, replacement)


def peel_composed_images_on_select(
    model_group: str,
    request_kwargs: dict[str, Any] | None,
    messages: list | None,
    *,
    protocol: ApiProtocol | None = None,
    logical: LogicalModelProtocols | None = None,
) -> None:
    """After account select: strip images on composed models, or fail closed.

    Vision compose translates on the async hang-point. Public sync select must
    not leak pixels to the execute model. ``S5_STUB_PEEL`` is ignored when
    vision compose is on.
    """
    if is_gateway_enhance_enabled() and is_vision_compose_enabled():
        from shared_quota_router.vision_async_flag import is_async_select

        if is_async_select():
            return
        if not defers_image_gate(model_group, logical):
            return
        kw_msgs = request_kwargs.get("messages") if isinstance(request_kwargs, dict) else None
        if messages_have_image(kw_msgs) or messages_have_image(messages):
            raise ProtocolAwareRoutingError(
                f"composed model {model_group!r} cannot translate images on the "
                "sync select path; use async_get_available_deployment",
                reason=ProtocolRoutingReason.FEATURE_UNSUPPORTED,
                protocol=protocol or ApiProtocol.ANTHROPIC_MESSAGES,
                model_group=model_group,
                details={"vision": "sync_path"},
            )
        return
    if not defers_image_gate(model_group, logical):
        return
    kw_msgs = request_kwargs.get("messages") if isinstance(request_kwargs, dict) else None
    found = messages_have_image(kw_msgs) or messages_have_image(messages)
    if not found:
        return
    if not stub_peel_enabled():
        raise ProtocolAwareRoutingError(
            f"composed model {model_group!r} still has image blocks; "
            "vision peel is disabled (S5 stub off, translator not mounted)",
            reason=ProtocolRoutingReason.FEATURE_UNSUPPORTED,
            protocol=protocol or ApiProtocol.ANTHROPIC_MESSAGES,
            model_group=model_group,
            details={"composed_peel": "disabled"},
        )
    replacement = _replacement_text()
    peel_messages(kw_msgs, replacement)
    if messages is not None and messages is not kw_msgs:
        peel_messages(messages, replacement)
