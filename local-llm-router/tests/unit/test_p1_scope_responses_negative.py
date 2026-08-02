"""P1-SCOPE：Messages→Chat native 不扩大 Responses 可达面（不撤销 Policy A）。"""

from __future__ import annotations

import pytest

from shared_quota_router.feature_flags import clear_flag_cache, set_g0a_messages_mount_ready
from shared_quota_router.models import (
    ApiProtocol,
    Deployment,
    Feature,
    LogicalModelProtocols,
)
from shared_quota_router.protocol_errors import ProtocolAwareRoutingError, ProtocolRoutingReason
from shared_quota_router.protocol_gates import assert_endpoint_allowed, public_reachable
from shared_quota_router.registry import DeploymentRegistry


@pytest.fixture(autouse=True)
def _reset(monkeypatch: pytest.MonkeyPatch) -> None:
    set_g0a_messages_mount_ready(False)
    monkeypatch.delenv("PROTOCOL_CONVERSION_ENABLED", raising=False)
    monkeypatch.delenv("PROTOCOL_AWARE_GATEWAY_ENABLED", raising=False)
    monkeypatch.delenv("SHARED_QUOTA_ENV_PROFILE", raising=False)
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


def test_p1_scope_messages_native_does_not_expand_responses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """开启 Messages→Chat native 时，未 opt-in Responses 的模型仍 protocol_not_enabled。"""
    monkeypatch.setenv("PROTOCOL_CONVERSION_ENABLED", "true")
    monkeypatch.setenv("PROTOCOL_AWARE_GATEWAY_ENABLED", "true")
    monkeypatch.setenv("SHARED_QUOTA_ENV_PROFILE", "staging")
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

    reg = DeploymentRegistry()
    reg.add(
        Deployment(
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
            # 仅 Anthropic Messages 公网 opt-in；无 Responses
            public_protocols=frozenset({ApiProtocol.ANTHROPIC_MESSAGES}),
            conversions=(),
        )
    )
    logical = {
        "kimi-k3": LogicalModelProtocols(
            model_group="kimi-k3",
            public_protocols=frozenset({ApiProtocol.ANTHROPIC_MESSAGES}),
            allow_conversion=True,
            allowed_conversions=frozenset(
                {(ApiProtocol.ANTHROPIC_MESSAGES, ApiProtocol.OPENAI_CHAT)}
            ),
        )
    }

    # Messages convert（非流）在 native 下可达 —— 证明 native 已开
    assert (
        public_reachable(
            model_group="kimi-k3",
            protocol=ApiProtocol.ANTHROPIC_MESSAGES,
            registry=reg,
            required_features=frozenset({Feature.TEXT}),
            stream=False,
            logical_models=logical,
        )
        is True
    )

    # Responses 仍不可达：不因 Messages native=true 扩大面
    assert (
        public_reachable(
            model_group="kimi-k3",
            protocol=ApiProtocol.OPENAI_RESPONSES,
            registry=reg,
            required_features=frozenset({Feature.TEXT}),
            stream=False,
            logical_models=logical,
        )
        is False
    )
    with pytest.raises(ProtocolAwareRoutingError) as ei:
        assert_endpoint_allowed(
            model_group="kimi-k3",
            protocol=ApiProtocol.OPENAI_RESPONSES,
            registry=reg,
            logical_models=logical,
        )
    assert ei.value.reason is ProtocolRoutingReason.UNSUPPORTED_PUBLIC_PROTOCOL
    assert ei.value.to_openai_error()["error"]["code"] == "protocol_not_enabled"
