"""C4: streaming conversion remains unsupported until evaluation Go.

P0-C4 / P1-C4-BYPASS：native Messages→Chat 统一 effective_stream；
streaming=False；gate 对 LITELLM_NATIVE 不强制 project converter。
"""

from __future__ import annotations

import pytest

from shared_quota_router.conversion.contracts import (
    DIRECTION_MESSAGES_TO_CHAT,
    feature_fidelity,
    validate_request_against_fidelity,
)
from shared_quota_router.feature_flags import clear_flag_cache, set_g0a_messages_mount_ready
from shared_quota_router.models import (
    ApiProtocol,
    Deployment,
    Feature,
    FidelityClass,
    LogicalModelProtocols,
    RouteMode,
    TransformOwner,
)
from shared_quota_router.protocol_errors import ProtocolAwareRoutingError
from shared_quota_router.protocol_gates import public_reachable
from shared_quota_router.registry import DeploymentRegistry


@pytest.fixture(autouse=True)
def _reset_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    set_g0a_messages_mount_ready(False)
    monkeypatch.delenv("PROTOCOL_CONVERSION_ENABLED", raising=False)
    monkeypatch.delenv("PROTOCOL_AWARE_GATEWAY_ENABLED", raising=False)
    monkeypatch.delenv(
        "LITELLM_USE_CHAT_COMPLETIONS_URL_FOR_ANTHROPIC_MESSAGES", raising=False
    )
    try:
        import litellm

        monkeypatch.setattr(
            litellm, "use_chat_completions_url_for_anthropic_messages", False
        )
    except ImportError:
        pass
    clear_flag_cache()
    yield
    set_g0a_messages_mount_ready(False)
    clear_flag_cache()


def _chat_only_dep() -> Deployment:
    """无 Anthropic upstream、无 project conversions —— 仅 native Messages→Chat。"""
    return Deployment(
        deployment_id="opencode-a-chat-kimi-k3",
        model_group="kimi-k3",
        upstream_model="openai/kimi-k3",
        provider_id="opencode-go",
        quota_group_id="opencode-a",
        priority=10,
        enabled=True,
        upstream_protocol=ApiProtocol.OPENAI_CHAT,
        supported_features=frozenset({Feature.TEXT}),
        supports_streaming=False,
        public_protocols=frozenset({ApiProtocol.ANTHROPIC_MESSAGES}),
        conversions=(),
    )


def _logical_convert() -> LogicalModelProtocols:
    return LogicalModelProtocols(
        model_group="kimi-k3",
        public_protocols=frozenset({ApiProtocol.ANTHROPIC_MESSAGES}),
        allow_conversion=True,
        allowed_conversions=frozenset(
            {(ApiProtocol.ANTHROPIC_MESSAGES, ApiProtocol.OPENAI_CHAT)}
        ),
    )


def _enable_native_convert(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PROTOCOL_CONVERSION_ENABLED", "true")
    monkeypatch.setenv("PROTOCOL_AWARE_GATEWAY_ENABLED", "true")
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


def test_c4_streaming_in_features_with_stream_false_still_rejects_native(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P1-C4-BYPASS：features 含 STREAMING 且 stream=False 仍拒绝 convert。"""
    from shared_quota_router.conversion.registry import resolve_route

    _enable_native_convert(monkeypatch)
    dep = _chat_only_dep()
    logical = _logical_convert()

    route = resolve_route(
        dep,
        public_protocol=ApiProtocol.ANTHROPIC_MESSAGES,
        required_features=frozenset({Feature.TEXT, Feature.STREAMING}),
        stream=False,
        logical=logical,
        conversion_enabled=True,
    )
    assert route is None

    reg = DeploymentRegistry()
    reg.add(dep)
    assert (
        public_reachable(
            model_group="kimi-k3",
            protocol=ApiProtocol.ANTHROPIC_MESSAGES,
            registry=reg,
            required_features=frozenset({Feature.TEXT, Feature.STREAMING}),
            stream=False,
            logical_models={"kimi-k3": logical},
        )
        is False
    )


def test_c4_native_convert_plus_stream_unreachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P0-C4：native Messages→Chat + stream 不可达。"""
    from shared_quota_router.conversion.registry import resolve_route

    _enable_native_convert(monkeypatch)
    dep = _chat_only_dep()
    logical = _logical_convert()

    route = resolve_route(
        dep,
        public_protocol=ApiProtocol.ANTHROPIC_MESSAGES,
        required_features=frozenset({Feature.TEXT}),
        stream=True,
        logical=logical,
        conversion_enabled=True,
    )
    assert route is None

    reg = DeploymentRegistry()
    reg.add(dep)
    assert (
        public_reachable(
            model_group="kimi-k3",
            protocol=ApiProtocol.ANTHROPIC_MESSAGES,
            registry=reg,
            required_features=frozenset({Feature.TEXT}),
            stream=True,
            logical_models={"kimi-k3": logical},
        )
        is False
    )


def test_c4_native_owner_non_stream_reachable_without_project_converter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P1-C4-BYPASS：native owner 非流式可达；gate 不依赖 get_converter。"""
    from shared_quota_router.conversion.registry import resolve_route
    from shared_quota_router.protocol_errors import ProtocolRoutingReason

    _enable_native_convert(monkeypatch)
    dep = _chat_only_dep()
    logical = _logical_convert()

    route = resolve_route(
        dep,
        public_protocol=ApiProtocol.ANTHROPIC_MESSAGES,
        required_features=frozenset({Feature.TEXT}),
        stream=False,
        logical=logical,
        conversion_enabled=True,
    )
    assert route is not None
    assert route.route_mode is RouteMode.CONVERT
    assert route.transform_owner is TransformOwner.LITELLM_NATIVE
    assert route.conversion is not None
    assert route.conversion.streaming is False

    # 即使 project converter 不可用，native 仍应 public_reachable
    def _boom(_direction: object) -> object:
        raise ProtocolAwareRoutingError(
            "no converter",
            reason=ProtocolRoutingReason.NO_COMPATIBLE_DEPLOYMENT,
            protocol=ApiProtocol.ANTHROPIC_MESSAGES,
        )

    monkeypatch.setattr(
        "shared_quota_router.conversion.dispatch.get_converter", _boom
    )
    reg = DeploymentRegistry()
    reg.add(dep)
    assert (
        public_reachable(
            model_group="kimi-k3",
            protocol=ApiProtocol.ANTHROPIC_MESSAGES,
            registry=reg,
            required_features=frozenset({Feature.TEXT}),
            stream=False,
            logical_models={"kimi-k3": logical},
        )
        is True
    )


@pytest.mark.skip(reason="C4 No-Go: streaming conversion adapter not implemented")
def test_c4_first_converted_visible_event_defines_first_byte() -> None:
    raise AssertionError("implement when streaming conversion is proven")
