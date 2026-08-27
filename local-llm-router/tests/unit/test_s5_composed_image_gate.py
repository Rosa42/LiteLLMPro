"""S5: composed models defer IMAGE until post-select peel; glm-5.2 stays closed."""

from __future__ import annotations

import pytest

from shared_quota_router.feature_flags import clear_flag_cache
from shared_quota_router.lease import LeaseManager
from shared_quota_router.mock_provider import MockHandler
from shared_quota_router.models import ApiProtocol, Deployment, Feature
from shared_quota_router.protocol_context import extract_required_features
from shared_quota_router.protocol_errors import (
    ProtocolAwareRoutingError,
    ProtocolRoutingReason,
)
from shared_quota_router.protocol_gates import enforce_pre_call_gates
from shared_quota_router.registry import DeploymentRegistry
from shared_quota_router.state_store import StateStore
from shared_quota_router.strategy import SharedQuotaRoutingStrategy

COMPOSED = "glm-5.2-vision"
EXEC = "glm-5.2"
MARKER = "P0B_S5UNIT01"
PNG_PLACEHOLDER = "s5pngpayload"

_TEXT_FEATURES = frozenset(
    {Feature.TEXT, Feature.STREAMING, Feature.TOOLS, Feature.REASONING}
)


@pytest.fixture(autouse=True)
def _enable_protocol_gateway(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PROTOCOL_AWARE_GATEWAY_ENABLED", "true")
    monkeypatch.delenv("S5_COMPOSED_MODELS", raising=False)
    monkeypatch.delenv("S5_STUB_PEEL", raising=False)
    monkeypatch.delenv("P0_PROBE_B_MARKER", raising=False)
    clear_flag_cache()
    yield
    clear_flag_cache()


class _MemRedis:
    def __init__(self) -> None:
        self.data: dict[str, str] = {}

    def get(self, name: str):
        return self.data.get(name)

    def set(self, name: str, value, ex=None, nx=False):
        if nx and name in self.data:
            return False
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
        keys = keys_and_args[:numkeys]
        args = keys_and_args[numkeys:]
        if numkeys == 3:
            inflight_key, lease_key = keys[1], keys[2]
            ttl, request_id = int(args[0]), args[2]
            inflight = self.incr(inflight_key)
            self.set(lease_key, request_id, ex=ttl)
            return [1, str(inflight)]
        if numkeys == 2:
            inflight_key, lease_key = keys
            self.delete(lease_key)
            inflight = int(self.data.get(inflight_key, "0"))
            if inflight > 0:
                inflight = self.decr(inflight_key)
            return inflight
        raise AssertionError("unexpected eval")


def _glm_deployment(model_group: str) -> Deployment:
    return Deployment(
        deployment_id=f"volc-c-msg-{model_group}",
        model_group=model_group,
        upstream_model="anthropic/glm-5.2",
        provider_id="volcengine",
        quota_group_id="volc-c",
        priority=20,
        upstream_protocol=ApiProtocol.ANTHROPIC_MESSAGES,
        supported_features=_TEXT_FEATURES,
        supports_streaming=True,
        public_protocols=frozenset({ApiProtocol.ANTHROPIC_MESSAGES}),
    )


def _image_messages(*, nested: bool = False) -> list[dict]:
    image = {
        "type": "image",
        "source": {"type": "base64", "media_type": "image/png", "data": PNG_PLACEHOLDER},
    }
    if nested:
        return [
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "t1",
                        "content": [image, {"type": "text", "text": "see"}],
                    }
                ],
            }
        ]
    return [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "describe"},
                dict(image),
            ],
        }
    ]


def _body(model: str, *, nested: bool = False) -> dict:
    return {
        "model": model,
        "messages": _image_messages(nested=nested),
    }


def _strategy_for(model_group: str) -> SharedQuotaRoutingStrategy:
    store = StateStore(_MemRedis())
    lease = LeaseManager(_MemRedis())
    model_list = [
        {
            "model_name": model_group,
            "model_info": {
                "deployment_id": f"volc-c-msg-{model_group}",
                "provider_id": "volcengine",
                "quota_group_id": "volc-c",
                "priority": 20,
                "enabled": True,
                "upstream_protocol": "anthropic_messages",
                "supported_features": ["text", "streaming", "tools", "reasoning"],
                "supports_streaming": True,
                "public_protocols": ["anthropic_messages"],
            },
            "litellm_params": {"model": "anthropic/glm-5.2"},
        }
    ]

    class Router:
        def __init__(self, ml: list) -> None:
            self.model_list = ml

    strat = SharedQuotaRoutingStrategy(store=store, lease_manager=lease)
    strat.bind_router(Router(model_list))
    return strat


def test_glm52_image_still_feature_unsupported() -> None:
    reg = DeploymentRegistry([_glm_deployment(EXEC)])
    with pytest.raises(ProtocolAwareRoutingError) as ei:
        enforce_pre_call_gates(
            _body(EXEC), call_type="anthropic_messages", registry=reg
        )
    assert ei.value.reason is ProtocolRoutingReason.FEATURE_UNSUPPORTED


def test_composed_image_pre_call_does_not_raise(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("S5_COMPOSED_MODELS", COMPOSED)
    reg = DeploymentRegistry([_glm_deployment(COMPOSED)])
    ctx = enforce_pre_call_gates(
        _body(COMPOSED), call_type="anthropic_messages", registry=reg
    )
    assert Feature.IMAGE in ctx.required_features
    assert ctx.protocol is ApiProtocol.ANTHROPIC_MESSAGES


def test_composed_stub_peel_strips_image_from_request_kwargs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("S5_COMPOSED_MODELS", COMPOSED)
    monkeypatch.setenv("S5_STUB_PEEL", "true")
    monkeypatch.setenv("P0_PROBE_B_MARKER", MARKER)
    kwargs = {
        "litellm_metadata": {"protocol": "anthropic_messages"},
        "messages": _image_messages(),
    }
    named = _image_messages()
    strat = _strategy_for(COMPOSED)
    strat.get_available_deployment(
        model=COMPOSED,
        messages=named,
        request_kwargs=kwargs,
    )
    for msgs in (kwargs["messages"], named):
        types = [b.get("type") for b in msgs[0]["content"] if isinstance(b, dict)]
        assert "image" not in types
        texts = [
            b.get("text", "")
            for b in msgs[0]["content"]
            if isinstance(b, dict) and b.get("type") == "text"
        ]
        joined = " ".join(str(t) for t in texts)
        assert MARKER in joined
        assert PNG_PLACEHOLDER not in joined


def test_composed_without_stub_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("S5_COMPOSED_MODELS", COMPOSED)
    monkeypatch.delenv("S5_STUB_PEEL", raising=False)
    kwargs = {
        "litellm_metadata": {"protocol": "anthropic_messages"},
        "messages": _image_messages(),
    }
    strat = _strategy_for(COMPOSED)
    with pytest.raises(ProtocolAwareRoutingError) as ei:
        strat.get_available_deployment(
            model=COMPOSED,
            messages=_image_messages(),
            request_kwargs=kwargs,
        )
    assert ei.value.reason is ProtocolRoutingReason.FEATURE_UNSUPPORTED
    assert ei.value.details.get("composed_peel") == "disabled"


def test_tool_result_nested_image_counts_as_image() -> None:
    feats = extract_required_features({"messages": _image_messages(nested=True)})
    assert Feature.IMAGE in feats


def test_mock_record_has_image_without_storing_bytes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("P0_PROBE_B_MARKER", MARKER)
    MockHandler.last_requests.clear()
    handler = MockHandler.__new__(MockHandler)
    handler.command = "POST"
    handler.headers = {"x-api-key": "sk-fake-never-store"}
    handler._record(
        "/v1/messages",
        {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {"type": "base64", "data": "iVBORw0KGgo"},
                        }
                    ],
                }
            ]
        },
    )
    rec = MockHandler.last_requests[-1]
    assert rec["has_image"] is True
    dumped = repr(MockHandler.last_requests)
    assert "iVBORw0KGgo" not in dumped
    assert "sk-fake-never-store" not in dumped
    handler._record(
        "/v1/messages",
        {"messages": [{"role": "user", "content": "pong"}]},
    )
    assert MockHandler.last_requests[-1]["has_image"] is False


def test_sync_select_fail_closed_when_vision_compose_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("S5_COMPOSED_MODELS", COMPOSED)
    monkeypatch.setenv("GATEWAY_ENHANCE_ENABLED", "true")
    monkeypatch.setenv("VISION_COMPOSE_ENABLED", "true")
    monkeypatch.delenv("S5_STUB_PEEL", raising=False)
    clear_flag_cache()
    kwargs = {
        "litellm_metadata": {"protocol": "anthropic_messages"},
        "messages": _image_messages(),
    }
    named = _image_messages()
    strat = _strategy_for(COMPOSED)
    with pytest.raises(ProtocolAwareRoutingError) as ei:
        strat.get_available_deployment(
            model=COMPOSED,
            messages=named,
            request_kwargs=kwargs,
        )
    assert ei.value.reason is ProtocolRoutingReason.FEATURE_UNSUPPORTED
    assert ei.value.details.get("vision") == "sync_path"
    types = [b.get("type") for b in named[0]["content"] if isinstance(b, dict)]
    assert "image" in types


@pytest.mark.asyncio
async def test_async_select_defers_peel_when_vision_compose_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("S5_COMPOSED_MODELS", COMPOSED)
    monkeypatch.setenv("GATEWAY_ENHANCE_ENABLED", "true")
    monkeypatch.setenv("VISION_COMPOSE_ENABLED", "true")
    monkeypatch.delenv("S5_STUB_PEEL", raising=False)
    clear_flag_cache()
    seen: list[str] = []

    async def spy(env) -> None:
        seen.append(env.model_group)
        from shared_quota_router.composed_vision import peel_messages

        peel_messages(env.messages, "<visual-evidence><pre>x</pre></visual-evidence>")

    monkeypatch.setattr("shared_quota_router.pipeline.run_pipeline", spy)
    kwargs = {
        "litellm_metadata": {"protocol": "anthropic_messages"},
        "messages": _image_messages(),
        "litellm_call_id": "async-vision",
    }
    named = _image_messages()
    strat = _strategy_for(COMPOSED)
    await strat.async_get_available_deployment(
        model=COMPOSED,
        messages=named,
        request_kwargs=kwargs,
    )
    assert seen == [COMPOSED]
    types = [b.get("type") for b in kwargs["messages"][0]["content"] if isinstance(b, dict)]
    assert "image" not in types
