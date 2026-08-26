"""Request protocol / feature context for pre-lease capability filtering (M2).

Rules:
- Never infer protocol from messages/input/model/provider/URL/prefix.
- Dual-bucket read: metadata (Chat) and litellm_metadata (Messages/Responses).
- Authoritative injection prefers call_type / route_type from pre-call hooks.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

from shared_quota_router.models import ApiProtocol, Feature, parse_api_protocol, parse_feature

# Proxy-internal call_type / route_type → public protocol (P0-verified).
_CALL_TYPE_TO_PROTOCOL: dict[str, ApiProtocol] = {
    "acompletion": ApiProtocol.OPENAI_CHAT,
    "completion": ApiProtocol.OPENAI_CHAT,
    "aresponses": ApiProtocol.OPENAI_RESPONSES,
    "responses": ApiProtocol.OPENAI_RESPONSES,
    "anthropic_messages": ApiProtocol.ANTHROPIC_MESSAGES,
}

# Which kwargs bucket LiteLLM uses after pre-call (P0 §4.2).
_CALL_TYPE_METADATA_BUCKET: dict[str, str] = {
    "acompletion": "metadata",
    "completion": "metadata",
    "aresponses": "litellm_metadata",
    "responses": "litellm_metadata",
    "anthropic_messages": "litellm_metadata",
}

PROTOCOL_META_KEY = "protocol"
FEATURES_META_KEY = "required_features"


@dataclass(frozen=True, slots=True)
class RequestProtocolContext:
    """Serializable protocol + feature requirements for one request."""

    protocol: ApiProtocol | None
    required_features: frozenset[Feature] = field(default_factory=frozenset)
    source: str = "none"  # metadata | litellm_metadata | call_type | none

    def as_wire_dict(self) -> dict[str, Any]:
        return {
            "protocol": self.protocol.value if self.protocol else None,
            "required_features": sorted(f.value for f in self.required_features),
            "source": self.source,
        }


def metadata_buckets(request_kwargs: Mapping[str, Any] | None) -> list[tuple[str, dict[str, Any]]]:
    """Return (bucket_name, dict) pairs present in kwargs (dual-key order)."""
    kwargs = request_kwargs or {}
    out: list[tuple[str, dict[str, Any]]] = []
    for name in ("metadata", "litellm_metadata"):
        bucket = kwargs.get(name)
        if isinstance(bucket, dict):
            out.append((name, bucket))
    # Nested under litellm_params (callback path)
    params = kwargs.get("litellm_params")
    if isinstance(params, dict):
        for name in ("metadata", "litellm_metadata"):
            bucket = params.get(name)
            if isinstance(bucket, dict):
                out.append((f"litellm_params.{name}", bucket))
    return out


def get_metadata_value(
    request_kwargs: Mapping[str, Any] | None,
    key: str,
) -> Any:
    """First non-empty value for ``key`` across dual metadata buckets."""
    for _name, bucket in metadata_buckets(request_kwargs):
        if key in bucket and bucket[key] not in (None, ""):
            return bucket[key]
    return None


def protocol_from_call_type(call_type: Any) -> ApiProtocol | None:
    if call_type is None:
        return None
    key = str(call_type).strip().lower()
    return _CALL_TYPE_TO_PROTOCOL.get(key)


def metadata_bucket_for_call_type(call_type: Any) -> str:
    if call_type is None:
        return "metadata"
    key = str(call_type).strip().lower()
    return _CALL_TYPE_METADATA_BUCKET.get(key, "metadata")


def extract_protocol_string(request_kwargs: Mapping[str, Any] | None) -> tuple[str | None, str]:
    """Return (protocol_wire, source_bucket). Does not infer from model/body shape."""
    for name, bucket in metadata_buckets(request_kwargs):
        raw = bucket.get(PROTOCOL_META_KEY)
        if raw is not None and str(raw).strip():
            return str(raw).strip(), name
    return None, "none"


def _scan_content_features(content: Any, features: set[Feature]) -> None:
    """Scan Anthropic content blocks, including nested ``tool_result`` (S5 / §7.3.1)."""
    if not isinstance(content, list):
        return
    for block in content:
        if not isinstance(block, dict):
            continue
        btype = str(block.get("type") or "")
        if btype in {"image", "image_url"}:
            features.add(Feature.IMAGE)
        elif btype == "thinking":
            features.add(Feature.REASONING)
        elif btype == "tool_use":
            features.add(Feature.TOOLS)
        elif btype == "tool_result":
            features.add(Feature.TOOLS)
            _scan_content_features(block.get("content"), features)


def extract_required_features(request_kwargs: Mapping[str, Any] | None) -> frozenset[Feature]:
    """Derive MVP feature requirements from explicit request fields + optional metadata.

    Does **not** infer protocol. Streaming/tools come from request kwargs flags.
    Explicit ``required_features`` in metadata (if present) is merged in.
    Content-block types (image/thinking/tool_use/tool_result) are scanned pre-lease (P1-04).
    ``tool_result.content`` is scanned recursively for nested images (S5 / §7.3.1).
    """
    kwargs = dict(request_kwargs or {})
    features: set[Feature] = {Feature.TEXT}

    stream = kwargs.get("stream")
    if stream is True or (isinstance(stream, str) and stream.lower() in {"true", "1", "yes"}):
        features.add(Feature.STREAMING)

    tools = kwargs.get("tools")
    if tools:
        features.add(Feature.TOOLS)

    # Anthropic / multimodal content blocks (pre-lease reject path)
    for msg in kwargs.get("messages") or []:
        if not isinstance(msg, dict):
            continue
        _scan_content_features(msg.get("content"), features)

    # Optional explicit list in either metadata bucket
    raw_list = get_metadata_value(kwargs, FEATURES_META_KEY)
    if isinstance(raw_list, Iterable) and not isinstance(raw_list, (str, bytes)):
        for item in raw_list:
            try:
                features.add(parse_feature(item))
            except ValueError:
                continue

    return frozenset(features)


def resolve_request_protocol_context(
    request_kwargs: Mapping[str, Any] | None,
    *,
    call_type: Any = None,
) -> RequestProtocolContext:
    """Build authoritative protocol context for candidate selection.

    Priority:
    1. Explicit ``protocol`` in metadata / litellm_metadata (never overwritten by inference)
    2. ``call_type`` / route_type mapping from pre-call hook
    3. None (caller decides legacy vs fail-closed)
    """
    features = extract_required_features(request_kwargs)
    raw, source = extract_protocol_string(request_kwargs)
    if raw is not None:
        try:
            return RequestProtocolContext(
                protocol=parse_api_protocol(raw),
                required_features=features,
                source=source,
            )
        except ValueError:
            # Unknown wire value → leave as unresolved; strategy raises config error
            return RequestProtocolContext(
                protocol=None,
                required_features=features,
                source=f"invalid:{source}",
            )

    from_call = protocol_from_call_type(call_type)
    if from_call is not None:
        return RequestProtocolContext(
            protocol=from_call,
            required_features=features,
            source="call_type",
        )

    # Also accept call_type stored in kwargs by our pre-call hook
    kwargs = request_kwargs or {}
    embedded = kwargs.get("shared_quota_call_type") or get_metadata_value(
        kwargs, "shared_quota_call_type"
    )
    from_embedded = protocol_from_call_type(embedded)
    if from_embedded is not None:
        return RequestProtocolContext(
            protocol=from_embedded,
            required_features=features,
            source="call_type",
        )

    return RequestProtocolContext(
        protocol=None,
        required_features=features,
        source="none",
    )


def inject_protocol_into_data(
    data: dict[str, Any],
    *,
    call_type: Any,
    overwrite: bool = False,
) -> dict[str, Any]:
    """Inject serializable protocol into the correct metadata bucket (pre-call).

    Mutates and returns ``data``. Safe when call_type is unknown (no-op).
    """
    protocol = protocol_from_call_type(call_type)
    if protocol is None:
        return data

    bucket_name = metadata_bucket_for_call_type(call_type)
    bucket = data.get(bucket_name)
    if not isinstance(bucket, dict):
        bucket = {}
        data[bucket_name] = bucket

    if overwrite or not bucket.get(PROTOCOL_META_KEY):
        bucket[PROTOCOL_META_KEY] = protocol.value

    # Keep a serializable breadcrumb for strategy dual-read / debugging
    data["shared_quota_call_type"] = str(call_type)
    bucket["shared_quota_call_type"] = str(call_type)
    return data
