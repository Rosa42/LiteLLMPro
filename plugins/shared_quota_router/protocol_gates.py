"""Public endpoint gates + pre-drop_params feature validation (M3).

Rules:
- Never enable Messages/Responses because a model name looks like Claude.
- Public exposure requires explicit ``public_protocols`` opt-in AND a verified
  ``upstream_protocol`` deployment for that logical model.
- Reject semantically required request features before LiteLLM ``drop_params``.
- Do not bridge Responses → Chat in MVP.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from shared_quota_router.models import ApiProtocol, Feature
from shared_quota_router.protocol_context import (
    RequestProtocolContext,
    extract_required_features,
    protocol_from_call_type,
    resolve_request_protocol_context,
)
from shared_quota_router.protocol_errors import (
    ProtocolAwareRoutingError,
    ProtocolRoutingReason,
)
from shared_quota_router.registry import DeploymentRegistry

logger = logging.getLogger(__name__)

# Params that are semantically required when present (must not be silently dropped).
_CHAT_REQUIRED_IF_PRESENT = frozenset(
    {
        "tools",
        "tool_choice",
        "functions",
        "function_call",
    }
)
_MESSAGES_REQUIRED_IF_PRESENT = frozenset(
    {
        "tools",
        "tool_choice",
    }
)
_RESPONSES_REQUIRED_IF_PRESENT = frozenset(
    {
        "tools",
        "tool_choice",
    }
)


def resolve_model_group(data: Mapping[str, Any] | None) -> str | None:
    if not data:
        return None
    model = data.get("model")
    if model is None or model == "":
        return None
    # Strip optional provider prefix for logical model lookup
    text = str(model)
    if "/" in text:
        # openai/kimi-k3 or anthropic/kimi-k3 → kimi-k3 when used as litellm model;
        # public clients usually send bare logical names.
        # Prefer bare name when it matches a known group; keep full string otherwise.
        return text.split("/", 1)[-1]
    return text


def public_reachable(
    *,
    model_group: str,
    protocol: ApiProtocol,
    registry: DeploymentRegistry,
    required_features: frozenset[Feature] | None = None,
    stream: bool = False,
    logical_models: Mapping[str, Any] | None = None,
) -> bool:
    """True when the public protocol can be served via direct OR conversion (P3).

    Conversion reachability requires dual flags + logical allowlist + registered
    adapter + feature-compatible conversion capability on an enabled deployment.
    Responses must never become reachable via conversion (C5).
    """
    from shared_quota_router.conversion.dispatch import get_converter
    from shared_quota_router.conversion.registry import resolve_route
    from shared_quota_router.feature_flags import is_conversion_routing_active
    from shared_quota_router.models import LogicalModelProtocols

    if registry.has_verified_upstream(model_group, protocol):
        # Direct path still needs feature capability when extras requested
        feats = required_features or frozenset({Feature.TEXT})
        if registry.filter_by_protocol(model_group, protocol):
            capable = [
                d
                for d in registry.filter_by_protocol(model_group, protocol)
                if all(d.supports_feature(f) for f in feats)
            ]
            if capable:
                return True
            # Fall through: maybe conversion can cover when direct lacks features
        else:
            return True

    # C5: Responses never via conversion
    if protocol is ApiProtocol.OPENAI_RESPONSES:
        return False

    if not is_conversion_routing_active():
        return False

    logical: LogicalModelProtocols | None = None
    if logical_models and model_group in logical_models:
        lm = logical_models[model_group]
        logical = lm if isinstance(lm, LogicalModelProtocols) else None
    if logical is None or not logical.allow_conversion:
        return False

    feats = required_features or frozenset({Feature.TEXT})
    for dep in registry.get_by_model_group(model_group):
        route = resolve_route(
            dep,
            public_protocol=protocol,
            required_features=feats,
            stream=stream,
            logical=logical,
            conversion_enabled=True,
        )
        if route is None or route.conversion is None:
            continue
        # Registered adapter must exist
        try:
            get_converter((route.conversion.source, route.conversion.target))
        except ProtocolAwareRoutingError:
            continue
        return True
    return False


def assert_endpoint_allowed(
    *,
    model_group: str,
    protocol: ApiProtocol,
    registry: DeploymentRegistry,
    required_features: frozenset[Feature] | None = None,
    stream: bool = False,
    logical_models: Mapping[str, Any] | None = None,
) -> None:
    """Hard gate for public protocol endpoints (M3-01..M3-03 + P3 convert).

    Raises ProtocolAwareRoutingError without mutating quota state.
    """
    from shared_quota_router.logical_policy import resolve_runtime_logical_models

    opted_in = registry.model_opts_into_public(model_group, protocol)
    lm_map = logical_models if logical_models is not None else resolve_runtime_logical_models()
    feats = required_features or frozenset({Feature.TEXT})
    reachable = public_reachable(
        model_group=model_group,
        protocol=protocol,
        registry=registry,
        required_features=feats,
        stream=stream,
        logical_models=lm_map,
    )

    if protocol is ApiProtocol.OPENAI_CHAT:
        if not opted_in:
            raise ProtocolAwareRoutingError(
                f"model {model_group!r} is not opted into public protocol "
                f"{protocol.value}",
                reason=ProtocolRoutingReason.UNSUPPORTED_PUBLIC_PROTOCOL,
                protocol=protocol,
                model_group=model_group,
            )
        if not reachable:
            raise ProtocolAwareRoutingError(
                f"no verified Chat deployment for model {model_group!r}",
                reason=ProtocolRoutingReason.NO_COMPATIBLE_DEPLOYMENT,
                protocol=protocol,
                model_group=model_group,
            )
        return

    # Messages / Responses: opt-in + reachable (direct or convert-eligible for Messages)
    if protocol is ApiProtocol.ANTHROPIC_MESSAGES:
        if not opted_in or not reachable:
            raise ProtocolAwareRoutingError(
                f"Anthropic Messages is not enabled for model {model_group!r} "
                f"(requires public_protocols opt-in and a verified "
                f"anthropic_messages deployment or active conversion route)",
                reason=ProtocolRoutingReason.UNSUPPORTED_PUBLIC_PROTOCOL,
                protocol=protocol,
                model_group=model_group,
            )
        return

    if protocol is ApiProtocol.OPENAI_RESPONSES:
        # Controlled disable until a verified Responses deployment + opt-in exist
        # Conversion must never unlock Responses (C5).
        has_upstream = registry.has_verified_upstream(model_group, protocol)
        if not opted_in or not has_upstream:
            raise ProtocolAwareRoutingError(
                f"OpenAI Responses is not enabled for model {model_group!r} "
                f"(MVP keeps /v1/responses disabled until a verified Responses "
                f"deployment is configured)",
                reason=ProtocolRoutingReason.UNSUPPORTED_PUBLIC_PROTOCOL,
                protocol=protocol,
                model_group=model_group,
            )
        return

    raise ProtocolAwareRoutingError(
        f"unsupported public protocol {protocol.value}",
        reason=ProtocolRoutingReason.CONFIGURATION_INVALID,
        protocol=protocol,
        model_group=model_group,
    )


def assert_chat_upstream_prefix(upstream_model: str, *, model_group: str) -> None:
    """M3-01: Chat-capable deployments must use openai/ adapter, never Responses path."""
    um = (upstream_model or "").strip().lower()
    if um.startswith("openai/"):
        return
    # anthropic/ on a Chat-declared deployment would misroute — fail closed
    raise ProtocolAwareRoutingError(
        f"Chat deployment for {model_group!r} has invalid upstream model prefix "
        f"{upstream_model!r}; expected openai/<model>",
        reason=ProtocolRoutingReason.CONFIGURATION_INVALID,
        protocol=ApiProtocol.OPENAI_CHAT,
        model_group=model_group,
        details={"upstream_model": upstream_model},
    )


def assert_required_features(
    *,
    model_group: str,
    protocol: ApiProtocol,
    required_features: frozenset[Feature],
    registry: DeploymentRegistry,
    logical_models: Mapping[str, Any] | None = None,
) -> None:
    """Reject when no direct or convert-eligible route supports required features."""
    from shared_quota_router.logical_policy import resolve_runtime_logical_models

    if not registry.model_opts_into_public(model_group, protocol):
        raise ProtocolAwareRoutingError(
            f"model {model_group!r} does not expose {protocol.value}",
            reason=ProtocolRoutingReason.UNSUPPORTED_PUBLIC_PROTOCOL,
            protocol=protocol,
            model_group=model_group,
        )

    stream = Feature.STREAMING in required_features
    lm_map = logical_models if logical_models is not None else resolve_runtime_logical_models()
    if public_reachable(
        model_group=model_group,
        protocol=protocol,
        registry=registry,
        required_features=required_features,
        stream=stream,
        logical_models=lm_map,
    ):
        return

    raise ProtocolAwareRoutingError(
        f"no deployment for {model_group!r} supports required features "
        f"{sorted(f.value for f in required_features)} on {protocol.value}",
        reason=ProtocolRoutingReason.FEATURE_UNSUPPORTED,
        protocol=protocol,
        model_group=model_group,
        details={
            "required_features": sorted(f.value for f in required_features),
        },
    )


def assert_required_params_not_silently_dropped(
    data: Mapping[str, Any],
    *,
    protocol: ApiProtocol,
    model_group: str,
    registry: DeploymentRegistry,
) -> None:
    """M3-04: if tools/stream are present, capability must exist before drop_params."""
    required = extract_required_features(dict(data))
    # Always includes TEXT; only enforce extra features when request needs them
    extras = required - {Feature.TEXT}
    if extras:
        assert_required_features(
            model_group=model_group,
            protocol=protocol,
            required_features=required,
            registry=registry,
        )

    keys = _required_keys_for_protocol(protocol)
    present = [k for k in keys if data.get(k) not in (None, "", [], {})]
    if not present:
        return

    # tools present ⇒ Feature.TOOLS must be supportable
    if any(k in present for k in ("tools", "tool_choice", "functions", "function_call")):
        assert_required_features(
            model_group=model_group,
            protocol=protocol,
            required_features=frozenset({Feature.TEXT, Feature.TOOLS}),
            registry=registry,
        )


def _required_keys_for_protocol(protocol: ApiProtocol) -> frozenset[str]:
    if protocol is ApiProtocol.ANTHROPIC_MESSAGES:
        return _MESSAGES_REQUIRED_IF_PRESENT
    if protocol is ApiProtocol.OPENAI_RESPONSES:
        return _RESPONSES_REQUIRED_IF_PRESENT
    return _CHAT_REQUIRED_IF_PRESENT


def enforce_pre_call_gates(
    data: dict[str, Any],
    *,
    call_type: Any,
    registry: DeploymentRegistry | None,
) -> RequestProtocolContext:
    """Inject protocol + enforce M3 endpoint/feature gates (when flag on).

    When ``PROTOCOL_AWARE_GATEWAY_ENABLED`` is false:
    - Chat Completions: legacy path (inject only; no public/feature gates)
    - Messages / Responses: still controlled disabled unless fully verified + opt-in

    When ``registry`` is None (tests / early startup), only injects protocol.
    """
    from shared_quota_router.feature_flags import is_protocol_aware_gateway_enabled
    from shared_quota_router.protocol_context import inject_protocol_into_data
    from shared_quota_router.protocol_observability import record_protocol_rejection

    inject_protocol_into_data(data, call_type=call_type, overwrite=False)
    inject_ok_ctx = resolve_request_protocol_context(data, call_type=call_type)
    protocol = inject_ok_ctx.protocol or protocol_from_call_type(call_type)
    if protocol is None:
        return inject_ok_ctx

    features = extract_required_features(data)
    ctx_out = RequestProtocolContext(
        protocol=protocol,
        required_features=features,
        source=inject_ok_ctx.source if inject_ok_ctx.protocol else "call_type",
    )

    if registry is None:
        return ctx_out

    model_group = resolve_model_group(data)
    if not model_group:
        raise ProtocolAwareRoutingError(
            "model is required for protocol-aware routing",
            reason=ProtocolRoutingReason.CONFIGURATION_INVALID,
            protocol=protocol,
            model_group=None,
        )

    aware = is_protocol_aware_gateway_enabled()
    # New public endpoints always stay controlled (even when gateway flag is off)
    always_gate = protocol in {
        ApiProtocol.ANTHROPIC_MESSAGES,
        ApiProtocol.OPENAI_RESPONSES,
    }

    if not aware and not always_gate:
        # Legacy Chat: no capability/public gates
        return ctx_out

    try:
        stream = Feature.STREAMING in features
        assert_endpoint_allowed(
            model_group=model_group,
            protocol=protocol,
            registry=registry,
            # Endpoint reachability is opt-in + path exists; feature depth is
            # enforced separately so unsupported tools stay FEATURE_UNSUPPORTED.
            required_features=frozenset({Feature.TEXT}),
            stream=stream,
        )
        if protocol is ApiProtocol.OPENAI_CHAT:
            for dep in registry.filter_by_protocol(model_group, protocol):
                assert_chat_upstream_prefix(dep.upstream_model, model_group=model_group)
        if aware:
            assert_required_params_not_silently_dropped(
                data,
                protocol=protocol,
                model_group=model_group,
                registry=registry,
            )
    except ProtocolAwareRoutingError as exc:
        record_protocol_rejection(
            public_protocol=exc.protocol or protocol,
            reason=exc.reason.value,
            model_group=model_group,
            route_mode="direct",
        )
        raise

    return ctx_out
