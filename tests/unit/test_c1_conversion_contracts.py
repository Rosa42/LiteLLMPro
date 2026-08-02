"""C1: conversion domain types, fidelity matrix, and route resolution."""

from __future__ import annotations

import pytest

from shared_quota_router.models import (
    ApiProtocol,
    ConversionCapability,
    Deployment,
    Feature,
    FidelityClass,
    RouteCandidate,
    RouteMode,
    parse_fidelity_class,
)


def test_fidelity_enum_values() -> None:
    assert FidelityClass.EQUIVALENT.value == "equivalent"
    assert FidelityClass.LOSSY_SAFE.value == "lossy_safe"
    assert FidelityClass.LOSSY_UNSAFE.value == "lossy_unsafe"
    assert FidelityClass.UNSUPPORTED.value == "unsupported"


def test_parse_fidelity_rejects_unknown() -> None:
    with pytest.raises(ValueError, match="unknown fidelity"):
        parse_fidelity_class("safe_for_text_tools_non_streaming")
    with pytest.raises(ValueError, match="invalid fidelity"):
        parse_fidelity_class("")
    assert parse_fidelity_class("EQUIVALENT") is FidelityClass.EQUIVALENT
    assert parse_fidelity_class(FidelityClass.LOSSY_SAFE) is FidelityClass.LOSSY_SAFE


def test_conversion_capability_direction_is_asymmetric() -> None:
    cap = ConversionCapability(
        source=ApiProtocol.ANTHROPIC_MESSAGES,
        target=ApiProtocol.OPENAI_CHAT,
        request_features=frozenset({Feature.TEXT}),
        response_features=frozenset({Feature.TEXT}),
        streaming=False,
        fidelity=FidelityClass.EQUIVALENT,
    )
    assert cap.source != cap.target
    assert cap.streaming is False
    assert cap.supports_request_features(frozenset({Feature.TEXT})) is True
    assert cap.supports_request_features(frozenset({Feature.TEXT, Feature.TOOLS})) is False
    assert RouteMode.CONVERT.value == "convert"
    assert RouteMode.DIRECT.value == "direct"


def test_post_mvp_features_exist_for_fidelity_matrix() -> None:
    assert Feature.REASONING.value == "reasoning"
    assert Feature.PROMPT_CACHE.value == "prompt_cache"
    assert Feature.STRUCTURED_OUTPUT.value == "structured_output"
    assert Feature.IMAGE.value == "image"
    assert Feature.PARALLEL_TOOL_CALLS.value == "parallel_tool_calls"
    assert Feature.CITATIONS.value == "citations"


def test_route_candidate_holds_deployment_and_mode() -> None:
    dep = Deployment(
        deployment_id="d1",
        model_group="pilot",
        upstream_model="openai/pilot",
        provider_id="p",
        quota_group_id="q",
        upstream_protocol=ApiProtocol.OPENAI_CHAT,
    )
    direct = RouteCandidate(deployment=dep, route_mode=RouteMode.DIRECT)
    assert direct.conversion is None
    cap = ConversionCapability(
        source=ApiProtocol.ANTHROPIC_MESSAGES,
        target=ApiProtocol.OPENAI_CHAT,
        request_features=frozenset({Feature.TEXT}),
        response_features=frozenset({Feature.TEXT}),
        streaming=False,
        fidelity=FidelityClass.EQUIVALENT,
    )
    convert = RouteCandidate(
        deployment=dep, route_mode=RouteMode.CONVERT, conversion=cap
    )
    assert convert.route_mode is RouteMode.CONVERT
    assert convert.conversion is cap


def test_deployment_conversions_default_empty() -> None:
    dep = Deployment(
        deployment_id="d1",
        model_group="m",
        upstream_model="openai/m",
        provider_id="p",
        quota_group_id="q",
    )
    assert dep.conversions == ()


def test_reasoning_is_lossy_unsafe_on_messages_to_chat() -> None:
    from shared_quota_router.conversion.contracts import (
        DIRECTION_MESSAGES_TO_CHAT,
        feature_fidelity,
    )

    assert (
        feature_fidelity(DIRECTION_MESSAGES_TO_CHAT, Feature.REASONING)
        is FidelityClass.LOSSY_UNSAFE
    )


def test_prompt_cache_unsupported_across_conversion() -> None:
    from shared_quota_router.conversion.contracts import (
        DIRECTION_MESSAGES_TO_CHAT,
        feature_fidelity,
    )

    assert (
        feature_fidelity(DIRECTION_MESSAGES_TO_CHAT, Feature.PROMPT_CACHE)
        is FidelityClass.UNSUPPORTED
    )


def test_text_only_non_streaming_accepted() -> None:
    from shared_quota_router.conversion.contracts import validate_request_against_fidelity

    validate_request_against_fidelity(
        source=ApiProtocol.ANTHROPIC_MESSAGES,
        target=ApiProtocol.OPENAI_CHAT,
        required_features=frozenset({Feature.TEXT}),
        stream=False,
    )


def test_tools_rejected_in_c2_pilot_matrix() -> None:
    from shared_quota_router.conversion.contracts import validate_request_against_fidelity
    from shared_quota_router.protocol_errors import ProtocolAwareRoutingError

    with pytest.raises(ProtocolAwareRoutingError) as ei:
        validate_request_against_fidelity(
            source=ApiProtocol.ANTHROPIC_MESSAGES,
            target=ApiProtocol.OPENAI_CHAT,
            required_features=frozenset({Feature.TEXT, Feature.TOOLS}),
            stream=False,
        )
    assert ei.value.reason.value == "feature_unsupported"


def test_streaming_rejected_until_c4() -> None:
    from shared_quota_router.conversion.contracts import validate_request_against_fidelity
    from shared_quota_router.protocol_errors import ProtocolAwareRoutingError

    with pytest.raises(ProtocolAwareRoutingError) as ei:
        validate_request_against_fidelity(
            source=ApiProtocol.ANTHROPIC_MESSAGES,
            target=ApiProtocol.OPENAI_CHAT,
            required_features=frozenset({Feature.TEXT}),
            stream=True,
        )
    assert ei.value.reason.value == "feature_unsupported"


def _chat_dep(**kwargs: object) -> Deployment:
    base = dict(
        deployment_id="chat-1",
        model_group="pilot",
        upstream_model="openai/pilot",
        provider_id="p",
        quota_group_id="q1",
        priority=10,
        enabled=True,
        upstream_protocol=ApiProtocol.OPENAI_CHAT,
        supported_features=frozenset({Feature.TEXT, Feature.STREAMING, Feature.TOOLS}),
        supports_streaming=True,
        public_protocols=frozenset(
            {ApiProtocol.OPENAI_CHAT, ApiProtocol.ANTHROPIC_MESSAGES}
        ),
    )
    base.update(kwargs)
    return Deployment(**base)  # type: ignore[arg-type]


def test_direct_wins_when_both_direct_and_convert_exist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from shared_quota_router.conversion.registry import resolve_route
    from shared_quota_router.feature_flags import clear_flag_cache
    from shared_quota_router.models import LogicalModelProtocols, TransformOwner

    # P0-G0A：Messages→Chat convert 仅 native；无 ADAPTER 回退
    try:
        import litellm

        monkeypatch.setattr(
            litellm, "use_chat_completions_url_for_anthropic_messages", True
        )
    except ImportError:
        monkeypatch.setenv(
            "LITELLM_USE_CHAT_COMPLETIONS_URL_FOR_ANTHROPIC_MESSAGES", "true"
        )
    clear_flag_cache()

    cap = ConversionCapability(
        source=ApiProtocol.ANTHROPIC_MESSAGES,
        target=ApiProtocol.OPENAI_CHAT,
        request_features=frozenset({Feature.TEXT}),
        response_features=frozenset({Feature.TEXT}),
        streaming=False,
        fidelity=FidelityClass.EQUIVALENT,
    )
    direct = _chat_dep(
        deployment_id="msg-direct",
        upstream_protocol=ApiProtocol.ANTHROPIC_MESSAGES,
        upstream_model="anthropic/pilot",
        conversions=(),
    )
    convert = _chat_dep(deployment_id="chat-convert", conversions=(cap,))
    logical = LogicalModelProtocols(
        model_group="pilot",
        public_protocols=frozenset(
            {ApiProtocol.ANTHROPIC_MESSAGES, ApiProtocol.OPENAI_CHAT}
        ),
        allow_conversion=True,
        allowed_conversions=frozenset(
            {(ApiProtocol.ANTHROPIC_MESSAGES, ApiProtocol.OPENAI_CHAT)}
        ),
    )
    r_direct = resolve_route(
        direct,
        public_protocol=ApiProtocol.ANTHROPIC_MESSAGES,
        required_features=frozenset({Feature.TEXT}),
        stream=False,
        logical=logical,
        conversion_enabled=True,
    )
    r_convert = resolve_route(
        convert,
        public_protocol=ApiProtocol.ANTHROPIC_MESSAGES,
        required_features=frozenset({Feature.TEXT}),
        stream=False,
        logical=logical,
        conversion_enabled=True,
    )
    assert r_direct is not None and r_direct.route_mode is RouteMode.DIRECT
    assert r_convert is not None and r_convert.route_mode is RouteMode.CONVERT
    assert r_convert.transform_owner is TransformOwner.LITELLM_NATIVE


def test_convert_candidate_only_when_policy_allows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from shared_quota_router.conversion.registry import resolve_route
    from shared_quota_router.feature_flags import clear_flag_cache
    from shared_quota_router.models import LogicalModelProtocols, TransformOwner

    try:
        import litellm

        monkeypatch.setattr(
            litellm, "use_chat_completions_url_for_anthropic_messages", True
        )
    except ImportError:
        monkeypatch.setenv(
            "LITELLM_USE_CHAT_COMPLETIONS_URL_FOR_ANTHROPIC_MESSAGES", "true"
        )
    clear_flag_cache()

    cap = ConversionCapability(
        source=ApiProtocol.ANTHROPIC_MESSAGES,
        target=ApiProtocol.OPENAI_CHAT,
        request_features=frozenset({Feature.TEXT}),
        response_features=frozenset({Feature.TEXT}),
        streaming=False,
        fidelity=FidelityClass.EQUIVALENT,
    )
    dep = _chat_dep(conversions=(cap,))
    logical = LogicalModelProtocols(
        model_group="pilot",
        public_protocols=frozenset({ApiProtocol.ANTHROPIC_MESSAGES}),
        allow_conversion=True,
        allowed_conversions=frozenset(
            {(ApiProtocol.ANTHROPIC_MESSAGES, ApiProtocol.OPENAI_CHAT)}
        ),
    )
    ok = resolve_route(
        dep,
        public_protocol=ApiProtocol.ANTHROPIC_MESSAGES,
        required_features=frozenset({Feature.TEXT}),
        stream=False,
        logical=logical,
        conversion_enabled=True,
    )
    assert ok is not None and ok.route_mode is RouteMode.CONVERT
    assert ok.transform_owner is TransformOwner.LITELLM_NATIVE

    blocked = resolve_route(
        dep,
        public_protocol=ApiProtocol.ANTHROPIC_MESSAGES,
        required_features=frozenset({Feature.TEXT}),
        stream=False,
        logical=logical,
        conversion_enabled=False,
    )
    assert blocked is None


def test_filter_route_candidates_orders_direct_before_convert(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from shared_quota_router.feature_flags import clear_flag_cache
    from shared_quota_router.models import LogicalModelProtocols
    from shared_quota_router.protocol_context import RequestProtocolContext
    from shared_quota_router.registry import DeploymentRegistry
    from shared_quota_router.state_store import StateStore
    from shared_quota_router.strategy import SharedQuotaSelector

    class _FakeRedis:
        def get(self, name: str) -> None:
            return None

        def set(self, *args: object, **kwargs: object) -> bool:
            return True

        def delete(self, *names: str) -> int:
            return 0

        def sadd(self, name: str, *values: object) -> int:
            return 0

        def smembers(self, name: str) -> set:
            return set()

        def expire(self, name: str, time: int) -> bool:
            return True

    monkeypatch.setenv("PROTOCOL_CONVERSION_ENABLED", "true")
    monkeypatch.setenv("PROTOCOL_AWARE_GATEWAY_ENABLED", "true")
    # P0-G0A：Messages→Chat path ready = native only
    try:
        import litellm

        monkeypatch.setattr(
            litellm, "use_chat_completions_url_for_anthropic_messages", True
        )
    except ImportError:
        monkeypatch.setenv(
            "LITELLM_USE_CHAT_COMPLETIONS_URL_FOR_ANTHROPIC_MESSAGES", "true"
        )
    clear_flag_cache()

    cap = ConversionCapability(
        source=ApiProtocol.ANTHROPIC_MESSAGES,
        target=ApiProtocol.OPENAI_CHAT,
        request_features=frozenset({Feature.TEXT}),
        response_features=frozenset({Feature.TEXT}),
        streaming=False,
        fidelity=FidelityClass.EQUIVALENT,
    )
    direct = _chat_dep(
        deployment_id="msg-direct",
        upstream_protocol=ApiProtocol.ANTHROPIC_MESSAGES,
        upstream_model="anthropic/pilot",
        priority=50,
    )
    convert = _chat_dep(deployment_id="chat-convert", conversions=(cap,), priority=1)
    reg = DeploymentRegistry()
    reg.add(direct)
    reg.add(convert)
    logical = LogicalModelProtocols(
        model_group="pilot",
        public_protocols=frozenset(
            {ApiProtocol.ANTHROPIC_MESSAGES, ApiProtocol.OPENAI_CHAT}
        ),
        allow_conversion=True,
        allowed_conversions=frozenset(
            {(ApiProtocol.ANTHROPIC_MESSAGES, ApiProtocol.OPENAI_CHAT)}
        ),
    )
    sel = SharedQuotaSelector(
        reg, StateStore(_FakeRedis()), logical_models={"pilot": logical}
    )
    ctx = RequestProtocolContext(
        protocol=ApiProtocol.ANTHROPIC_MESSAGES,
        required_features=frozenset({Feature.TEXT}),
        source="test",
    )
    routes = sel.filter_route_candidates("pilot", ctx)
    assert [r.route_mode for r in routes] == [RouteMode.DIRECT, RouteMode.CONVERT]
    assert routes[0].deployment.deployment_id == "msg-direct"
