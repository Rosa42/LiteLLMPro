"""B5 P0-DEP: per-deployment streaming capability filtering."""

from __future__ import annotations

import pytest

from shared_quota_router.conversion.registry import resolve_route
from shared_quota_router.models import (
    ApiProtocol,
    Deployment,
    Feature,
    LogicalModelProtocols,
    RouteMode,
)
from shared_quota_router.protocol_errors import ProtocolAwareRoutingError
from shared_quota_router.protocol_gates import enforce_pre_call_gates
from shared_quota_router.registry import DeploymentRegistry


def _dep(
    dep_id: str,
    model: str,
    *,
    qg: str = "qg",
    streaming: bool = False,
    protocol: ApiProtocol = ApiProtocol.ANTHROPIC_MESSAGES,
) -> Deployment:
    feats = frozenset({Feature.TEXT, Feature.STREAMING}) if streaming else frozenset({Feature.TEXT})
    return Deployment(
        deployment_id=dep_id,
        model_group=model,
        upstream_model=f"anthropic/{model}",
        provider_id="p",
        quota_group_id=qg,
        upstream_protocol=protocol,
        supported_features=feats,
        supports_streaming=streaming,
        public_protocols=frozenset({protocol}),
    )


def test_stream_selects_only_streaming_deployment_for_same_model() -> None:
    open_msg = _dep("opencode-a-msg-glm-5.2", "glm-5.2", streaming=False)
    volc_msg = _dep("volc-c-msg-glm-5.2", "glm-5.2", qg="volc", streaming=True)
    lm = LogicalModelProtocols.from_config("glm-5.2", ["anthropic_messages"])
    required = frozenset({Feature.TEXT, Feature.STREAMING})

    assert resolve_route(
        open_msg,
        public_protocol=ApiProtocol.ANTHROPIC_MESSAGES,
        required_features=required,
        stream=True,
        logical=lm,
        conversion_enabled=False,
    ) is None
    route = resolve_route(
        volc_msg,
        public_protocol=ApiProtocol.ANTHROPIC_MESSAGES,
        required_features=required,
        stream=True,
        logical=lm,
        conversion_enabled=False,
    )
    assert route is not None
    assert route.deployment.deployment_id == "volc-c-msg-glm-5.2"


def test_non_stream_still_uses_non_streaming_deployment() -> None:
    dep = _dep("opencode-a-msg-glm-5.2", "glm-5.2", streaming=False)
    lm = LogicalModelProtocols.from_config("glm-5.2", ["anthropic_messages"])
    route = resolve_route(
        dep,
        public_protocol=ApiProtocol.ANTHROPIC_MESSAGES,
        required_features=frozenset({Feature.TEXT}),
        stream=False,
        logical=lm,
        conversion_enabled=False,
    )
    assert route is not None
    assert route.route_mode is RouteMode.DIRECT


def test_chat_plan_per_model_streaming_independent() -> None:
    deepseek = Deployment(
        deployment_id="opencode-a-chat-deepseek-v4-flash",
        model_group="deepseek-v4-flash",
        upstream_model="openai/deepseek-v4-flash",
        provider_id="p",
        quota_group_id="opencode-a",
        upstream_protocol=ApiProtocol.OPENAI_CHAT,
        supported_features=frozenset({Feature.TEXT, Feature.STREAMING}),
        supports_streaming=True,
        public_protocols=frozenset({ApiProtocol.OPENAI_CHAT}),
    )
    kimi = Deployment(
        deployment_id="opencode-a-chat-kimi-k3",
        model_group="kimi-k3",
        upstream_model="openai/kimi-k3",
        provider_id="p",
        quota_group_id="opencode-a",
        upstream_protocol=ApiProtocol.OPENAI_CHAT,
        supported_features=frozenset({Feature.TEXT}),
        supports_streaming=False,
        public_protocols=frozenset({ApiProtocol.OPENAI_CHAT, ApiProtocol.ANTHROPIC_MESSAGES}),
    )
    lm_ds = LogicalModelProtocols.from_config(
        "deepseek-v4-flash", ["openai_chat", "anthropic_messages"]
    )
    lm_kimi = LogicalModelProtocols.from_config(
        "kimi-k3", ["openai_chat", "anthropic_messages"], allow_conversion=True
    )
    assert resolve_route(
        deepseek,
        public_protocol=ApiProtocol.OPENAI_CHAT,
        required_features=frozenset({Feature.TEXT, Feature.STREAMING}),
        stream=True,
        logical=lm_ds,
        conversion_enabled=True,
    ) is not None
    assert (
        resolve_route(
            kimi,
            public_protocol=ApiProtocol.OPENAI_CHAT,
            required_features=frozenset({Feature.TEXT, Feature.STREAMING}),
            stream=True,
            logical=lm_kimi,
            conversion_enabled=True,
        )
        is None
    )


def test_convert_stream_still_blocked() -> None:
    from shared_quota_router.models import ConversionCapability, FidelityClass

    chat = Deployment(
        deployment_id="opencode-a-chat-kimi-k3",
        model_group="kimi-k3",
        upstream_model="openai/kimi-k3",
        provider_id="p",
        quota_group_id="opencode-a",
        upstream_protocol=ApiProtocol.OPENAI_CHAT,
        supported_features=frozenset({Feature.TEXT, Feature.STREAMING}),
        supports_streaming=True,
        public_protocols=frozenset({ApiProtocol.OPENAI_CHAT}),
        conversions=(
            ConversionCapability(
                source=ApiProtocol.ANTHROPIC_MESSAGES,
                target=ApiProtocol.OPENAI_CHAT,
                request_features=frozenset({Feature.TEXT}),
                response_features=frozenset({Feature.TEXT}),
                streaming=False,
                fidelity=FidelityClass.LOSSY_SAFE,
            ),
        ),
    )
    lm = LogicalModelProtocols.from_config(
        "kimi-k3",
        ["anthropic_messages", "openai_chat"],
        allow_conversion=True,
        allowed_conversions=frozenset(
            {(ApiProtocol.ANTHROPIC_MESSAGES, ApiProtocol.OPENAI_CHAT)}
        ),
    )
    assert (
        resolve_route(
            chat,
            public_protocol=ApiProtocol.ANTHROPIC_MESSAGES,
            required_features=frozenset({Feature.TEXT, Feature.STREAMING}),
            stream=True,
            logical=lm,
            conversion_enabled=True,
        )
        is None
    )


def test_stream_request_no_capable_deployment_hard_reject_gate() -> None:
    dep = _dep("opencode-a-msg-glm-5.2", "glm-5.2", streaming=False)
    reg = DeploymentRegistry([dep])
    data = {
        "model": "glm-5.2",
        "stream": True,
        "messages": [{"role": "user", "content": "hi"}],
        "litellm_metadata": {"protocol": "anthropic_messages"},
    }
    with pytest.raises(ProtocolAwareRoutingError):
        enforce_pre_call_gates(data, call_type="anthropic_messages", registry=reg)
