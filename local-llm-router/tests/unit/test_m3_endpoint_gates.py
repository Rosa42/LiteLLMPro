"""M3: public endpoint gates + drop_params / feature validation."""

from __future__ import annotations

import asyncio

import pytest
from shared_quota_router.callbacks import SharedQuotaCallback
from shared_quota_router.feature_flags import clear_flag_cache
from shared_quota_router.generator import render_litellm_yaml
from shared_quota_router.models import (
    ApiProtocol,
    Deployment,
    Feature,
)
from shared_quota_router.protocol_errors import (
    ProtocolAwareRoutingError,
    ProtocolRoutingReason,
)
from shared_quota_router.protocol_gates import (
    assert_chat_upstream_prefix,
    assert_endpoint_allowed,
    assert_required_features,
    enforce_pre_call_gates,
)
from shared_quota_router.registry import DeploymentRegistry, deployment_from_model_entry
from shared_quota_router.state_store import StateStore


@pytest.fixture(autouse=True)
def _enable_protocol_gateway(monkeypatch: pytest.MonkeyPatch) -> None:
    """M3 gates require PROTOCOL_AWARE_GATEWAY_ENABLED (M4-02 default is false)."""
    monkeypatch.setenv("PROTOCOL_AWARE_GATEWAY_ENABLED", "true")
    clear_flag_cache()
    yield
    clear_flag_cache()


class MemRedis:
    def __init__(self) -> None:
        self.data: dict[str, str] = {}

    def get(self, name: str):
        return self.data.get(name)

    def set(self, name: str, value, ex=None, nx=False):
        self.data[name] = value if isinstance(value, str) else str(value)
        return True

    def delete(self, *names: str):
        for n in names:
            self.data.pop(n, None)

    def incr(self, name: str):
        v = int(self.data.get(name, "0")) + 1
        self.data[name] = str(v)
        return v

    def decr(self, name: str):
        v = int(self.data.get(name, "0")) - 1
        self.data[name] = str(v)
        return v

    def expire(self, name: str, time: int):
        return True

    def eval(self, script: str, numkeys: int, *keys_and_args):
        return [1, "1"] if numkeys == 3 else 0


def _chat_reg() -> DeploymentRegistry:
    return DeploymentRegistry(
        [
            Deployment(
                deployment_id="opencode-a-kimi",
                model_group="kimi-k3",
                upstream_model="openai/kimi-k3",
                provider_id="opencode-go",
                quota_group_id="opencode-a",
                priority=10,
                upstream_protocol=ApiProtocol.OPENAI_CHAT,
                supported_features=frozenset(
                    {Feature.TEXT, Feature.STREAMING, Feature.TOOLS}
                ),
                supports_streaming=True,
                public_protocols=frozenset({ApiProtocol.OPENAI_CHAT}),
            ),
            Deployment(
                deployment_id="volc-c-glm",
                model_group="glm-5.2",
                upstream_model="openai/glm-5.2",
                provider_id="volcengine",
                quota_group_id="volc-c",
                priority=30,
                upstream_protocol=ApiProtocol.OPENAI_CHAT,
                supported_features=frozenset(
                    {Feature.TEXT, Feature.STREAMING, Feature.TOOLS}
                ),
                supports_streaming=True,
                public_protocols=frozenset({ApiProtocol.OPENAI_CHAT}),
            ),
        ]
    )


# ----- M3-01 Chat -----


def test_m3_01_chat_opt_in_allowed() -> None:
    reg = _chat_reg()
    assert_endpoint_allowed(
        model_group="kimi-k3",
        protocol=ApiProtocol.OPENAI_CHAT,
        registry=reg,
    )


def test_m3_01_chat_requires_openai_prefix() -> None:
    assert_chat_upstream_prefix("openai/kimi-k3", model_group="kimi-k3")
    with pytest.raises(ProtocolAwareRoutingError) as ei:
        assert_chat_upstream_prefix("anthropic/kimi-k3", model_group="kimi-k3")
    assert ei.value.reason is ProtocolRoutingReason.CONFIGURATION_INVALID


def test_m3_01_pre_call_chat_ok() -> None:
    reg = _chat_reg()
    data = {"model": "kimi-k3", "messages": [{"role": "user", "content": "hi"}]}
    ctx = enforce_pre_call_gates(data, call_type="acompletion", registry=reg)
    assert ctx.protocol is ApiProtocol.OPENAI_CHAT
    assert data["metadata"]["protocol"] == "openai_chat"


def test_m3_01_registry_parses_public_protocols() -> None:
    dep = deployment_from_model_entry(
        {
            "model_name": "kimi-k3",
            "litellm_params": {"model": "openai/kimi-k3"},
            "model_info": {
                "deployment_id": "d1",
                "upstream_protocol": "openai_chat",
                "public_protocols": ["openai_chat"],
                "supported_features": ["text", "streaming", "tools"],
                "supports_streaming": True,
            },
        }
    )
    assert dep.public_protocols == frozenset({ApiProtocol.OPENAI_CHAT})
    assert dep.publicly_exposes(ApiProtocol.OPENAI_CHAT)


# ----- M3-02 Messages -----


def test_m3_02_messages_disabled_without_opt_in() -> None:
    reg = _chat_reg()
    with pytest.raises(ProtocolAwareRoutingError) as ei:
        assert_endpoint_allowed(
            model_group="kimi-k3",
            protocol=ApiProtocol.ANTHROPIC_MESSAGES,
            registry=reg,
        )
    assert ei.value.reason is ProtocolRoutingReason.UNSUPPORTED_PUBLIC_PROTOCOL
    body = ei.value.to_anthropic_error()
    assert body["type"] == "error"


def test_m3_02_messages_not_enabled_by_claude_name() -> None:
    """Model name must never imply Messages capability."""
    reg = DeploymentRegistry(
        [
            Deployment(
                deployment_id="claude-a",
                model_group="claude-opus-4-8",
                upstream_model="openai/claude-opus-4-8",
                provider_id="newapi",
                quota_group_id="newapi-a",
                enabled=False,
                # no upstream_protocol, no public_protocols
            )
        ]
    )
    with pytest.raises(ProtocolAwareRoutingError):
        assert_endpoint_allowed(
            model_group="claude-opus-4-8",
            protocol=ApiProtocol.ANTHROPIC_MESSAGES,
            registry=reg,
        )


def test_m3_02_messages_enabled_when_verified() -> None:
    reg = DeploymentRegistry(
        [
            Deployment(
                deployment_id="msg-a",
                model_group="kimi-k3",
                upstream_model="anthropic/kimi-k3",
                provider_id="newapi",
                quota_group_id="newapi-a",
                upstream_protocol=ApiProtocol.ANTHROPIC_MESSAGES,
                supported_features=frozenset({Feature.TEXT, Feature.STREAMING}),
                supports_streaming=True,
                public_protocols=frozenset({ApiProtocol.ANTHROPIC_MESSAGES}),
            )
        ]
    )
    assert_endpoint_allowed(
        model_group="kimi-k3",
        protocol=ApiProtocol.ANTHROPIC_MESSAGES,
        registry=reg,
    )


def test_m3_02_callback_pre_call_rejects_messages() -> None:
    cb = SharedQuotaCallback(store=StateStore(MemRedis()), registry=_chat_reg())
    data = {"model": "kimi-k3", "messages": [{"role": "user", "content": "hi"}]}

    async def _run():
        return await cb.async_pre_call_hook(data=data, call_type="anthropic_messages")

    with pytest.raises(ProtocolAwareRoutingError) as ei:
        asyncio.run(_run())
    assert ei.value.protocol is ApiProtocol.ANTHROPIC_MESSAGES


# ----- M3-03 Responses -----


def test_m3_03_responses_disabled_by_default() -> None:
    reg = _chat_reg()
    with pytest.raises(ProtocolAwareRoutingError) as ei:
        assert_endpoint_allowed(
            model_group="kimi-k3",
            protocol=ApiProtocol.OPENAI_RESPONSES,
            registry=reg,
        )
    assert ei.value.reason is ProtocolRoutingReason.UNSUPPORTED_PUBLIC_PROTOCOL
    assert ei.value.to_openai_error()["error"]["code"] == "protocol_not_enabled"


def test_m3_03_responses_not_bridged_to_chat_deployments() -> None:
    """Chat-only upstream must never satisfy Responses gate."""
    reg = _chat_reg()
    data = {"model": "kimi-k3", "input": "hi"}
    with pytest.raises(ProtocolAwareRoutingError):
        enforce_pre_call_gates(data, call_type="aresponses", registry=reg)


def test_m3_03_responses_enable_with_verified_deployment() -> None:
    reg = DeploymentRegistry(
        [
            Deployment(
                deployment_id="resp-a",
                model_group="kimi-k3",
                upstream_model="openai/kimi-k3",
                provider_id="mock-responses",
                quota_group_id="mock-a",
                upstream_protocol=ApiProtocol.OPENAI_RESPONSES,
                supported_features=frozenset({Feature.TEXT, Feature.STREAMING}),
                supports_streaming=True,
                public_protocols=frozenset({ApiProtocol.OPENAI_RESPONSES}),
            )
        ]
    )
    assert_endpoint_allowed(
        model_group="kimi-k3",
        protocol=ApiProtocol.OPENAI_RESPONSES,
        registry=reg,
    )


# ----- M3-04 drop_params / features -----


def test_m3_04_tools_rejected_when_unsupported() -> None:
    reg = DeploymentRegistry(
        [
            Deployment(
                deployment_id="no-tools",
                model_group="kimi-k3",
                upstream_model="openai/kimi-k3",
                provider_id="opencode-go",
                quota_group_id="a",
                upstream_protocol=ApiProtocol.OPENAI_CHAT,
                supported_features=frozenset({Feature.TEXT, Feature.STREAMING}),
                supports_streaming=True,
                public_protocols=frozenset({ApiProtocol.OPENAI_CHAT}),
            )
        ]
    )
    data = {
        "model": "kimi-k3",
        "tools": [{"type": "function", "function": {"name": "x"}}],
    }
    with pytest.raises(ProtocolAwareRoutingError) as ei:
        enforce_pre_call_gates(data, call_type="acompletion", registry=reg)
    assert ei.value.reason is ProtocolRoutingReason.FEATURE_UNSUPPORTED


def test_m3_04_stream_rejected_when_unsupported() -> None:
    reg = DeploymentRegistry(
        [
            Deployment(
                deployment_id="no-stream",
                model_group="kimi-k3",
                upstream_model="openai/kimi-k3",
                provider_id="opencode-go",
                quota_group_id="a",
                upstream_protocol=ApiProtocol.OPENAI_CHAT,
                supported_features=frozenset({Feature.TEXT, Feature.TOOLS}),
                supports_streaming=False,
                public_protocols=frozenset({ApiProtocol.OPENAI_CHAT}),
            )
        ]
    )
    data = {"model": "kimi-k3", "stream": True}
    with pytest.raises(ProtocolAwareRoutingError) as ei:
        enforce_pre_call_gates(data, call_type="acompletion", registry=reg)
    assert ei.value.reason is ProtocolRoutingReason.FEATURE_UNSUPPORTED


def test_m3_04_valid_chat_tools_ok() -> None:
    reg = _chat_reg()
    data = {
        "model": "kimi-k3",
        "stream": True,
        "tools": [{"type": "function", "function": {"name": "x"}}],
    }
    ctx = enforce_pre_call_gates(data, call_type="acompletion", registry=reg)
    assert Feature.TOOLS in ctx.required_features
    assert Feature.STREAMING in ctx.required_features


# ----- MiniMax-M3 IMAGE (Task 3; Probe A PASS) -----

_M3_TEXT_FEATURES = frozenset(
    {Feature.TEXT, Feature.STREAMING, Feature.TOOLS, Feature.REASONING}
)
_M3_IMAGE_FEATURES = _M3_TEXT_FEATURES | {Feature.IMAGE}

_M3_IMAGE_BLOCK = {
    "type": "image",
    "source": {"type": "base64", "media_type": "image/png", "data": "x"},
}


def _m3_deployment(*, features: frozenset[Feature]) -> Deployment:
    return Deployment(
        deployment_id="minimax-official-msg-MiniMax-M3",
        model_group="MiniMax-M3",
        upstream_model="anthropic/MiniMax-M3",
        provider_id="minimax",
        quota_group_id="minimax-official",
        priority=25,
        upstream_protocol=ApiProtocol.ANTHROPIC_MESSAGES,
        supported_features=features,
        supports_streaming=Feature.STREAMING in features,
        public_protocols=frozenset({ApiProtocol.ANTHROPIC_MESSAGES}),
    )


def _m3_messages_image_body() -> dict:
    return {
        "model": "MiniMax-M3",
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "hi"},
                    dict(_M3_IMAGE_BLOCK),
                ],
            }
        ],
    }


def test_m3_image_assert_required_features_ok_when_supported() -> None:
    reg = DeploymentRegistry([_m3_deployment(features=_M3_IMAGE_FEATURES)])
    assert_required_features(
        model_group="MiniMax-M3",
        protocol=ApiProtocol.ANTHROPIC_MESSAGES,
        required_features=frozenset({Feature.TEXT, Feature.IMAGE}),
        registry=reg,
    )


def test_m3_image_assert_required_features_rejected_when_unsupported() -> None:
    reg = DeploymentRegistry([_m3_deployment(features=_M3_TEXT_FEATURES)])
    with pytest.raises(ProtocolAwareRoutingError) as ei:
        assert_required_features(
            model_group="MiniMax-M3",
            protocol=ApiProtocol.ANTHROPIC_MESSAGES,
            required_features=frozenset({Feature.TEXT, Feature.IMAGE}),
            registry=reg,
        )
    assert ei.value.reason is ProtocolRoutingReason.FEATURE_UNSUPPORTED


def test_m3_image_pre_call_rejected_when_unsupported() -> None:
    reg = DeploymentRegistry([_m3_deployment(features=_M3_TEXT_FEATURES)])
    data = _m3_messages_image_body()
    with pytest.raises(ProtocolAwareRoutingError) as ei:
        enforce_pre_call_gates(data, call_type="anthropic_messages", registry=reg)
    assert ei.value.reason is ProtocolRoutingReason.FEATURE_UNSUPPORTED


def test_m3_image_pre_call_ok_when_supported() -> None:
    reg = DeploymentRegistry([_m3_deployment(features=_M3_IMAGE_FEATURES)])
    data = _m3_messages_image_body()
    ctx = enforce_pre_call_gates(data, call_type="anthropic_messages", registry=reg)
    assert Feature.IMAGE in ctx.required_features
    assert ctx.protocol is ApiProtocol.ANTHROPIC_MESSAGES


def test_m3_04_generator_drop_params_false() -> None:
    from pathlib import Path

    from shared_quota_router.config_schema import load_plans_file

    plans = Path(__file__).resolve().parents[2] / "config" / "plans.example.yaml"
    if not plans.exists():
        pytest.skip("plans.example.yaml missing")
    doc = load_plans_file(plans)
    yaml_text = render_litellm_yaml(doc)
    assert "drop_params: false" in yaml_text
    assert "drop_params: true" not in yaml_text
