"""P0-1 / §11 runtime: trusted internal select, IMAGE features, outcome report."""

from __future__ import annotations

from typing import Any

import pytest

from shared_quota_router.callbacks import SharedQuotaCallback
from shared_quota_router.feature_flags import clear_flag_cache
from shared_quota_router.internal_call import (
    is_trusted_internal,
    report_internal_outcome,
    select_internal_deployment,
    trusted_internal,
)
from shared_quota_router.lease import LeaseManager
from shared_quota_router.models import ApiProtocol, Feature
from shared_quota_router.pipeline import is_internal_call
from shared_quota_router.protocol_errors import (
    ProtocolAwareRoutingError,
    ProtocolRoutingReason,
)
from shared_quota_router.state_store import StateStore
from shared_quota_router.strategy import SharedQuotaRoutingStrategy


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

    def sadd(self, name: str, *values):
        return True

    def smembers(self, name: str):
        return set()

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
            request_id = args[0] if args else ""
            current = self.data.get(lease_key)
            if "EXPIRE" in script:
                if current != request_id:
                    return 0
                return 1
            self.delete(lease_key)
            inflight = int(self.data.get(inflight_key, "0"))
            if inflight > 0:
                inflight = self.decr(inflight_key)
            return inflight
        raise AssertionError("unexpected eval")


def _entry(
    *,
    model_name: str,
    deployment_id: str,
    quota_group_id: str,
    features: list[str],
    public: list[str] | None,
    priority: int = 20,
) -> dict[str, Any]:
    info: dict[str, Any] = {
        "deployment_id": deployment_id,
        "provider_id": "minimax",
        "quota_group_id": quota_group_id,
        "priority": priority,
        "enabled": True,
        "upstream_protocol": "anthropic_messages",
        "supported_features": features,
        "supports_streaming": True,
    }
    if public is not None:
        info["public_protocols"] = public
    return {
        "model_name": model_name,
        "model_info": info,
        "litellm_params": {
            "model": f"anthropic/{model_name}",
            "api_base": "https://api.minimaxi.com/anthropic",
            "api_key": "secret",
        },
    }


def _strategy(model_list: list[dict[str, Any]]) -> SharedQuotaRoutingStrategy:
    store = StateStore(_MemRedis())
    lease = LeaseManager(_MemRedis())

    class Router:
        def __init__(self, ml: list) -> None:
            self.model_list = ml

    strat = SharedQuotaRoutingStrategy(store=store, lease_manager=lease)
    strat.bind_router(Router(model_list))
    return strat


@pytest.fixture(autouse=True)
def _flags(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PROTOCOL_AWARE_GATEWAY_ENABLED", "true")
    monkeypatch.delenv("GATEWAY_ENHANCE_ENABLED", raising=False)
    monkeypatch.delenv("VISION_COMPOSE_ENABLED", raising=False)
    clear_flag_cache()
    yield
    clear_flag_cache()


def test_is_internal_call_ignores_client_metadata() -> None:
    kwargs = {
        "internal_call": True,
        "litellm_metadata": {"internal_call": True},
        "metadata": {"internal_call": "true"},
    }
    assert is_internal_call(kwargs) is False
    assert is_trusted_internal() is False
    with trusted_internal():
        assert is_trusted_internal() is True
        assert is_internal_call({}) is True
        assert is_internal_call(kwargs) is True
    assert is_trusted_internal() is False
    assert is_internal_call(kwargs) is False


def test_public_select_rejects_private_translator_even_with_internal_call_metadata() -> None:
    strat = _strategy(
        [
            _entry(
                model_name="MiniMax-M3",
                deployment_id="mm-private",
                quota_group_id="minimax-official",
                features=["text", "streaming", "tools", "image"],
                public=[],
            )
        ]
    )
    kwargs = {
        "litellm_call_id": "public-1",
        "messages": [{"role": "user", "content": "hi"}],
        "litellm_metadata": {
            "protocol": "anthropic_messages",
            "internal_call": True,
        },
        "metadata": {"protocol": "anthropic_messages", "internal_call": True},
    }
    with pytest.raises(ProtocolAwareRoutingError) as ei:
        strat.get_available_deployment(
            "MiniMax-M3",
            messages=kwargs["messages"],
            request_kwargs=kwargs,
        )
    assert ei.value.reason is ProtocolRoutingReason.UNSUPPORTED_PUBLIC_PROTOCOL


def test_internal_select_allows_private_translator() -> None:
    strat = _strategy(
        [
            _entry(
                model_name="MiniMax-M3",
                deployment_id="mm-private",
                quota_group_id="minimax-official",
                features=["text", "streaming", "tools", "image"],
                public=[],
            )
        ]
    )
    entry = select_internal_deployment(
        "MiniMax-M3",
        select=strat.get_available_deployment,
        protocol=ApiProtocol.ANTHROPIC_MESSAGES,
        required_features=frozenset({Feature.TEXT, Feature.IMAGE}),
        parent_request_id="parent-1",
        parent_quota_group_id="volc-c",
        child_id="parent-1#vision:deadbeef",
    )
    assert entry["model_info"]["deployment_id"] == "mm-private"
    assert is_trusted_internal() is False


def test_internal_select_requires_image_feature() -> None:
    strat = _strategy(
        [
            _entry(
                model_name="MiniMax-M3",
                deployment_id="mm-text",
                quota_group_id="minimax-text",
                features=["text", "streaming", "tools"],
                public=[],
                priority=10,
            ),
            _entry(
                model_name="MiniMax-M3",
                deployment_id="mm-image",
                quota_group_id="minimax-official",
                features=["text", "streaming", "tools", "image"],
                public=[],
                priority=90,
            ),
        ]
    )
    text_only = select_internal_deployment(
        "MiniMax-M3",
        select=strat.get_available_deployment,
        required_features=frozenset({Feature.TEXT}),
        parent_request_id="p",
        parent_quota_group_id="volc-c",
        child_id="p#vision:aaa",
    )
    assert text_only["model_info"]["deployment_id"] == "mm-text"

    with_image = select_internal_deployment(
        "MiniMax-M3",
        select=strat.get_available_deployment,
        required_features=frozenset({Feature.TEXT, Feature.IMAGE}),
        parent_request_id="p",
        parent_quota_group_id="volc-c",
        child_id="p#vision:bbb",
    )
    assert with_image["model_info"]["deployment_id"] == "mm-image"


def test_report_internal_outcome_429_cools_deployment() -> None:
    store = StateStore(_MemRedis())
    cb = SharedQuotaCallback(store=store, lease_manager=None)
    kwargs = {
        "litellm_call_id": "parent#vision:deadbeef",
        "model_info": {
            "deployment_id": "mm-1",
            "quota_group_id": "minimax-official",
            "provider_id": "minimax",
        },
        "litellm_params": {
            "model_info": {
                "deployment_id": "mm-1",
                "quota_group_id": "minimax-official",
                "provider_id": "minimax",
            }
        },
    }
    report_internal_outcome(kwargs, success=False, status_code=429, callback=cb)
    st = store.get_deployment_state("mm-1")
    assert st is not None and st.is_in_cooldown is True


def test_report_internal_outcome_success_clears_failures() -> None:
    store = StateStore(_MemRedis())
    cb = SharedQuotaCallback(store=store, lease_manager=None)
    kwargs = {
        "litellm_call_id": "parent#vision:cafe",
        "model_info": {
            "deployment_id": "mm-1",
            "quota_group_id": "minimax-official",
            "provider_id": "minimax",
        },
    }
    report_internal_outcome(kwargs, success=False, status_code=429, callback=cb)
    report_internal_outcome(kwargs, success=True, callback=cb)
    st = store.get_deployment_state("mm-1")
    assert st is not None
    assert st.is_in_cooldown is False
    group = store.get_quota_group("minimax-official")
    assert group is not None
    assert group.consecutive_failures == 0


@pytest.mark.asyncio
async def test_async_select_runs_pipeline_despite_client_internal_call_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GATEWAY_ENHANCE_ENABLED", "true")
    clear_flag_cache()
    calls: list[str] = []

    async def spy(env: Any) -> None:
        calls.append("ran")

    monkeypatch.setattr("shared_quota_router.pipeline.run_pipeline", spy)
    strat = _strategy(
        [
            _entry(
                model_name="glm-5.2",
                deployment_id="volc-c-msg-glm-5.2",
                quota_group_id="volc-c",
                features=["text", "streaming", "tools", "reasoning"],
                public=["anthropic_messages"],
            )
        ]
    )
    kwargs = {
        "litellm_metadata": {
            "protocol": "anthropic_messages",
            "internal_call": True,
        },
        "messages": [{"role": "user", "content": "hi"}],
        "litellm_call_id": "public-spoof-internal-select",
    }
    await strat.async_get_available_deployment(
        model="glm-5.2",
        messages=[{"role": "user", "content": "hi"}],
        request_kwargs=kwargs,
    )
    assert calls == ["ran"]


@pytest.mark.asyncio
async def test_async_select_skips_pipeline_when_trusted_internal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GATEWAY_ENHANCE_ENABLED", "true")
    clear_flag_cache()
    calls: list[str] = []

    async def spy(env: Any) -> None:
        calls.append("ran")

    monkeypatch.setattr("shared_quota_router.pipeline.run_pipeline", spy)
    strat = _strategy(
        [
            _entry(
                model_name="MiniMax-M3",
                deployment_id="mm-private",
                quota_group_id="minimax-official",
                features=["text", "streaming", "tools", "image"],
                public=[],
            )
        ]
    )
    with trusted_internal():
        await strat.async_get_available_deployment(
            model="MiniMax-M3",
            messages=[{"role": "user", "content": "hi"}],
            request_kwargs={
                "litellm_call_id": "trusted-skip-pipeline-1",
                "litellm_metadata": {
                    "protocol": "anthropic_messages",
                    "required_features": ["text", "image"],
                },
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
    assert calls == []
