"""Phase 1–3 remaining-dev-plan correctness tests."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from shared_quota_router.feature_flags import clear_flag_cache, set_g0a_messages_mount_ready
from shared_quota_router.models import (
    ApiProtocol,
    ConversionCapability,
    Deployment,
    Feature,
    FidelityClass,
    LogicalModelProtocols,
)
from shared_quota_router.protocol_context import extract_required_features
from shared_quota_router.protocol_errors import (
    ProtocolAwareRoutingError,
    ProtocolRoutingReason,
)
from shared_quota_router.protocol_gates import assert_endpoint_allowed, public_reachable
from shared_quota_router.registry import DeploymentRegistry
from shared_quota_router.state_store import StateStore
from shared_quota_router.strategy import (
    SharedQuotaRoutingStrategy,
    model_list_to_registry,
)


class Mem:
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

    def sadd(self, *a, **k):
        return 1

    def smembers(self, name: str):
        return set()

    def eval(self, script, numkeys, *args):
        if numkeys == 3:
            self.incr(args[1])
            return [1, "1"]
        return 0


def _chat_convert_model_list() -> list[dict]:
    return [
        {
            "model_name": "pilot",
            "model_info": {
                "deployment_id": "chat-convert",
                "provider_id": "p",
                "quota_group_id": "q1",
                "priority": 10,
                "enabled": True,
                "upstream_protocol": "openai_chat",
                "supported_features": ["text", "streaming", "tools"],
                "supports_streaming": True,
                "public_protocols": ["anthropic_messages", "openai_chat"],
                "conversions": [
                    {
                        "from": "anthropic_messages",
                        "to": "openai_chat",
                        "fidelity": "equivalent",
                        "streaming": False,
                        "features": {"request": ["text"], "response": ["text"]},
                    }
                ],
            },
            "litellm_params": {"model": "openai/pilot", "api_base": "http://x"},
        }
    ]


def test_p1_01_selector_gets_logical_models_without_monkeypatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PROTOCOL_CONVERSION_ENABLED", "true")
    monkeypatch.setenv("PROTOCOL_AWARE_GATEWAY_ENABLED", "true")
    from shared_quota_router.feature_flags import set_g0a_messages_mount_ready
    set_g0a_messages_mount_ready(True)
    clear_flag_cache()

    from shared_quota_router.lease import LeaseManager

    logical = {
        "pilot": LogicalModelProtocols(
            model_group="pilot",
            public_protocols=frozenset(
                {ApiProtocol.ANTHROPIC_MESSAGES, ApiProtocol.OPENAI_CHAT}
            ),
            allow_conversion=True,
            allowed_conversions=frozenset(
                {(ApiProtocol.ANTHROPIC_MESSAGES, ApiProtocol.OPENAI_CHAT)}
            ),
        )
    }
    store = StateStore(Mem())
    lease = LeaseManager(Mem())
    model_list = _chat_convert_model_list()

    class Router:
        def __init__(self, ml: list) -> None:
            self.model_list = ml

    strat = SharedQuotaRoutingStrategy(
        store=store, lease_manager=lease, logical_models=logical
    )
    strat.bind_router(Router(model_list))
    kwargs = {
        "litellm_metadata": {"protocol": "anthropic_messages"},
        "model": "pilot",
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 8,
    }
    entry = strat.get_available_deployment(
        model="pilot",
        messages=[{"role": "user", "content": "hi"}],
        request_kwargs=kwargs,
    )
    assert entry["model_info"]["deployment_id"] == "chat-convert"
    assert kwargs["litellm_metadata"]["shared_quota_route_mode"] == "convert"
    assert kwargs["messages"][0]["role"] in {"user", "system"}


def test_p1_02_generator_emits_logical_models_roundtrip() -> None:
    from shared_quota_router.config_schema import load_plans_dict
    from shared_quota_router.generator import render_litellm_yaml
    from shared_quota_router.logical_policy import parse_logical_models_section
    import yaml

    doc = load_plans_dict(
        {
            "plans": [
                {
                    "id": "p1",
                    "display_name": "P",
                    "provider_id": "opencode-go",
                    "priority": 10,
                    "base_url_env": "OPENCODE_GO_BASE_URL",
                    "api_key_env": "OPENCODE_GO_KEY_A",
                    "upstream_protocol": "openai_chat",
                    "supported_features": ["text", "streaming"],
                    "models": ["pilot"],
                    "conversions": [
                        {
                            "from": "anthropic_messages",
                            "to": "openai_chat",
                            "fidelity": "equivalent",
                            "streaming": False,
                            "features": {"request": ["text"], "response": ["text"]},
                        }
                    ],
                }
            ],
            "logical_models": {
                "pilot": {
                    "public_protocols": ["openai_chat", "anthropic_messages"],
                    "allow_conversion": True,
                    "conversion_policy": {
                        "allowed": [
                            {"from": "anthropic_messages", "to": "openai_chat"}
                        ]
                    },
                }
            },
        }
    )
    text = render_litellm_yaml(doc)
    assert "shared_quota_logical_models:" in text
    parsed = yaml.safe_load(text)
    lm = parse_logical_models_section(parsed["shared_quota_logical_models"])
    assert lm["pilot"].allow_conversion is True
    assert (
        ApiProtocol.ANTHROPIC_MESSAGES,
        ApiProtocol.OPENAI_CHAT,
    ) in lm["pilot"].allowed_conversions


def test_p1_03_protocol_error_releases_lease() -> None:
    from shared_quota_router.callbacks import SharedQuotaCallback
    from shared_quota_router.lease import LeaseManager

    redis = Mem()

    def eval_lease(script, numkeys, *args):
        # Minimal acquire/release simulation for lease keys
        if numkeys == 3:
            # acquire: KEYS status, inflight, lease
            lease_key = args[2]
            inflight_key = args[1]
            redis.incr(inflight_key)
            redis.set(lease_key, args[5] if len(args) > 5 else "1")
            return [1, "1"]
        if numkeys == 2:
            inflight_key, lease_key = args[0], args[1]
            redis.delete(lease_key)
            if inflight_key in redis.data:
                redis.decr(inflight_key)
            return 1
        return 0

    redis.eval = eval_lease  # type: ignore[method-assign]
    store = StateStore(redis)
    lease = LeaseManager(redis)
    assert lease.acquire(quota_group_id="q1", request_id="req-proto") is True
    assert any("lease" in k for k in redis.data)

    cb = SharedQuotaCallback(store=store, lease_manager=lease)
    exc = ProtocolAwareRoutingError(
        "tools unsupported",
        reason=ProtocolRoutingReason.FEATURE_UNSUPPORTED,
        protocol=ApiProtocol.ANTHROPIC_MESSAGES,
        model_group="pilot",
    )
    cb.on_failure(
        {
            "exception": exc,
            "litellm_call_id": "req-proto",
            "model_info": {
                "deployment_id": "d1",
                "quota_group_id": "q1",
                "provider_id": "p",
            },
        },
        exc,
    )
    assert not any(k for k in redis.data if ":lease:" in k)
    assert store.get_quota_group("q1") is None


def test_p1_04_content_blocks_extracted_pre_lease() -> None:
    feats = extract_required_features(
        {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "hi"},
                        {"type": "image", "source": {}},
                    ],
                }
            ]
        }
    )
    assert Feature.IMAGE in feats


def test_p1_05_optional_params_are_declared_dropped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PROTOCOL_CONVERSION_ENABLED", "true")
    monkeypatch.setenv("PROTOCOL_AWARE_GATEWAY_ENABLED", "true")
    from shared_quota_router.feature_flags import set_g0a_messages_mount_ready
    set_g0a_messages_mount_ready(True)
    clear_flag_cache()
    from shared_quota_router.conversion.adapters.messages_to_chat import (
        MessagesToChatConverter,
    )
    from shared_quota_router.conversion.dispatch import convert_public_request
    from shared_quota_router.conversion.contracts import DIRECTION_MESSAGES_TO_CHAT

    out = MessagesToChatConverter().convert_request(
        {
            "model": "x",
            "messages": [{"role": "user", "content": "hi"}],
            "temperature": 0.2,
        }
    )
    assert "temperature" in out.dropped_fields
    with pytest.raises(ProtocolAwareRoutingError) as ei:
        convert_public_request(
            {
                "model": "x",
                "messages": [{"role": "user", "content": "hi"}],
                "temperature": 0.2,
            },
            direction=DIRECTION_MESSAGES_TO_CHAT,
        )
    assert ei.value.reason is ProtocolRoutingReason.FEATURE_UNSUPPORTED


def test_p3_conversion_only_messages_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PROTOCOL_CONVERSION_ENABLED", "true")
    monkeypatch.setenv("PROTOCOL_AWARE_GATEWAY_ENABLED", "true")
    from shared_quota_router.feature_flags import set_g0a_messages_mount_ready
    set_g0a_messages_mount_ready(True)
    clear_flag_cache()

    cap = ConversionCapability(
        source=ApiProtocol.ANTHROPIC_MESSAGES,
        target=ApiProtocol.OPENAI_CHAT,
        request_features=frozenset({Feature.TEXT}),
        response_features=frozenset({Feature.TEXT}),
        streaming=False,
        fidelity=FidelityClass.EQUIVALENT,
    )
    dep = Deployment(
        deployment_id="chat-only",
        model_group="pilot",
        upstream_model="openai/pilot",
        provider_id="p",
        quota_group_id="q1",
        priority=10,
        enabled=True,
        upstream_protocol=ApiProtocol.OPENAI_CHAT,
        supported_features=frozenset({Feature.TEXT, Feature.STREAMING}),
        supports_streaming=True,
        public_protocols=frozenset(
            {ApiProtocol.ANTHROPIC_MESSAGES, ApiProtocol.OPENAI_CHAT}
        ),
        conversions=(cap,),
    )
    reg = DeploymentRegistry()
    reg.add(dep)
    logical = {
        "pilot": LogicalModelProtocols(
            model_group="pilot",
            public_protocols=frozenset(
                {ApiProtocol.ANTHROPIC_MESSAGES, ApiProtocol.OPENAI_CHAT}
            ),
            allow_conversion=True,
            allowed_conversions=frozenset(
                {(ApiProtocol.ANTHROPIC_MESSAGES, ApiProtocol.OPENAI_CHAT)}
            ),
        )
    }
    assert (
        public_reachable(
            model_group="pilot",
            protocol=ApiProtocol.ANTHROPIC_MESSAGES,
            registry=reg,
            logical_models=logical,
        )
        is True
    )
    assert_endpoint_allowed(
        model_group="pilot",
        protocol=ApiProtocol.ANTHROPIC_MESSAGES,
        registry=reg,
        logical_models=logical,
    )

    monkeypatch.setenv("PROTOCOL_CONVERSION_ENABLED", "false")
    clear_flag_cache()
    assert (
        public_reachable(
            model_group="pilot",
            protocol=ApiProtocol.ANTHROPIC_MESSAGES,
            registry=reg,
            logical_models=logical,
        )
        is False
    )


def test_p3_responses_never_via_conversion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PROTOCOL_CONVERSION_ENABLED", "true")
    monkeypatch.setenv("PROTOCOL_AWARE_GATEWAY_ENABLED", "true")
    from shared_quota_router.feature_flags import set_g0a_messages_mount_ready
    set_g0a_messages_mount_ready(True)
    clear_flag_cache()
    reg = DeploymentRegistry()
    reg.add(
        Deployment(
            deployment_id="chat",
            model_group="pilot",
            upstream_model="openai/x",
            provider_id="p",
            quota_group_id="q1",
            priority=10,
            enabled=True,
            upstream_protocol=ApiProtocol.OPENAI_CHAT,
            supported_features=frozenset({Feature.TEXT}),
            supports_streaming=False,
            public_protocols=frozenset({ApiProtocol.OPENAI_RESPONSES}),
            conversions=(),
        )
    )
    assert (
        public_reachable(
            model_group="pilot",
            protocol=ApiProtocol.OPENAI_RESPONSES,
            registry=reg,
            logical_models={},
        )
        is False
    )


def test_p3_conversion_only_denied_without_path_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CONVERSION=true 但无 native/g0a 时，conversion-only 不可达。"""
    monkeypatch.setenv("PROTOCOL_CONVERSION_ENABLED", "true")
    monkeypatch.setenv("PROTOCOL_AWARE_GATEWAY_ENABLED", "true")
    set_g0a_messages_mount_ready(False)
    try:
        import litellm

        monkeypatch.setattr(
            litellm, "use_chat_completions_url_for_anthropic_messages", False
        )
    except ImportError:
        pass
    clear_flag_cache()

    cap = ConversionCapability(
        source=ApiProtocol.ANTHROPIC_MESSAGES,
        target=ApiProtocol.OPENAI_CHAT,
        request_features=frozenset({Feature.TEXT}),
        response_features=frozenset({Feature.TEXT}),
        streaming=False,
        fidelity=FidelityClass.EQUIVALENT,
    )
    reg = DeploymentRegistry()
    reg.add(
        Deployment(
            deployment_id="chat-only",
            model_group="pilot",
            upstream_model="openai/pilot",
            provider_id="p",
            quota_group_id="q1",
            priority=10,
            enabled=True,
            upstream_protocol=ApiProtocol.OPENAI_CHAT,
            supported_features=frozenset({Feature.TEXT}),
            supports_streaming=False,
            public_protocols=frozenset({ApiProtocol.ANTHROPIC_MESSAGES}),
            conversions=(cap,),
        )
    )
    logical = {
        "pilot": LogicalModelProtocols(
            model_group="pilot",
            public_protocols=frozenset({ApiProtocol.ANTHROPIC_MESSAGES}),
            allow_conversion=True,
            allowed_conversions=frozenset(
                {(ApiProtocol.ANTHROPIC_MESSAGES, ApiProtocol.OPENAI_CHAT)}
            ),
        )
    }
    assert (
        public_reachable(
            model_group="pilot",
            protocol=ApiProtocol.ANTHROPIC_MESSAGES,
            registry=reg,
            logical_models=logical,
        )
        is False
    )


@pytest.mark.asyncio
async def test_p2_failure_hook_returns_http_exception_for_protocol_error() -> None:
    pytest.importorskip("fastapi")
    from fastapi import HTTPException

    from shared_quota_router.callbacks import SharedQuotaCallback

    cb = SharedQuotaCallback(store=StateStore(Mem()))
    exc = ProtocolAwareRoutingError(
        "not enabled",
        reason=ProtocolRoutingReason.UNSUPPORTED_PUBLIC_PROTOCOL,
        protocol=ApiProtocol.ANTHROPIC_MESSAGES,
        model_group="pilot",
    )
    result = await cb.async_post_call_failure_hook(
        {"model": "pilot"},
        exc,
    )
    assert isinstance(result, HTTPException)
    assert result.status_code == 400
    assert result.detail["type"] == "error"
