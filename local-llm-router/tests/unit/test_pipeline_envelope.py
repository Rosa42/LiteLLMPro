"""F1: enhance pipeline envelope and async hang-point (no vision/memory stages)."""

from __future__ import annotations

from typing import Any

import pytest

from shared_quota_router.feature_flags import clear_flag_cache
from shared_quota_router.lease import LeaseManager
from shared_quota_router.state_store import StateStore
from shared_quota_router.strategy import SharedQuotaRoutingStrategy

MODEL = "glm-5.2"


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


def _strategy() -> SharedQuotaRoutingStrategy:
    store = StateStore(_MemRedis())
    lease = LeaseManager(_MemRedis())
    model_list = [
        {
            "model_name": MODEL,
            "model_info": {
                "deployment_id": "volc-c-msg-glm-5.2",
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


@pytest.fixture(autouse=True)
def _clear_enhance_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PROTOCOL_AWARE_GATEWAY_ENABLED", "true")
    monkeypatch.delenv("GATEWAY_ENHANCE_ENABLED", raising=False)
    monkeypatch.delenv("VISION_COMPOSE_ENABLED", raising=False)
    monkeypatch.delenv("GATEWAY_MEMORY_ENABLED", raising=False)
    monkeypatch.delenv("GATEWAY_MEMORY_EXTRACT_ENABLED", raising=False)
    clear_flag_cache()
    yield
    clear_flag_cache()


def _envelope(**overrides: Any):
    from shared_quota_router.pipeline import EnhanceEnvelope

    base: dict[str, Any] = {
        "model_group": MODEL,
        "protocol": None,
        "streaming": False,
        "messages": [{"role": "user", "content": "hi"}],
        "workspace": None,
        "visual_evidence": [],
        "memory_hits": [],
        "internal_call": False,
        "parent_request_id": "r1",
        "parent_quota_group_id": "volc-c",
        "stage_ms": {},
    }
    base.update(overrides)
    return EnhanceEnvelope(**base)


@pytest.mark.asyncio
async def test_pipeline_noop_when_enhance_disabled() -> None:
    from shared_quota_router.pipeline import run_pipeline

    env = _envelope()
    original = list(env.messages)
    await run_pipeline(env)
    assert env.messages == original
    assert env.stage_ms == {}


@pytest.mark.asyncio
async def test_pipeline_skips_when_internal_call(monkeypatch: pytest.MonkeyPatch) -> None:
    from shared_quota_router.pipeline import run_pipeline

    monkeypatch.setenv("GATEWAY_ENHANCE_ENABLED", "true")
    clear_flag_cache()
    env = _envelope(model_group="glm-5.2-vision", messages=[], internal_call=True)
    await run_pipeline(env)
    assert env.stage_ms == {}


@pytest.mark.asyncio
async def test_pipeline_noop_stages_do_not_mutate_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from shared_quota_router.pipeline import run_pipeline

    monkeypatch.setenv("GATEWAY_ENHANCE_ENABLED", "true")
    clear_flag_cache()
    env = _envelope()
    original = list(env.messages)
    await run_pipeline(env)
    assert env.messages == original
    assert env.stage_ms == {}


def test_enhance_flags_default_false_in_snapshot() -> None:
    from shared_quota_router.feature_flags import (
        flag_snapshot,
        is_gateway_enhance_enabled,
        is_gateway_memory_enabled,
        is_gateway_memory_extract_enabled,
        is_vision_agent_fingerprints_enabled,
        is_vision_compose_enabled,
    )

    snap = flag_snapshot()
    assert is_gateway_enhance_enabled() is False
    assert is_vision_compose_enabled() is False
    assert is_vision_agent_fingerprints_enabled() is True
    assert is_gateway_memory_enabled() is False
    assert is_gateway_memory_extract_enabled() is False
    assert snap["GATEWAY_ENHANCE_ENABLED"] is False
    assert snap["VISION_COMPOSE_ENABLED"] is False
    assert snap["VISION_AGENT_FINGERPRINTS"] is True
    assert snap["GATEWAY_MEMORY_ENABLED"] is False
    assert snap["GATEWAY_MEMORY_EXTRACT_ENABLED"] is False


def test_extract_flag_ignored_when_memory_off(monkeypatch: pytest.MonkeyPatch) -> None:
    from shared_quota_router.feature_flags import (
        is_gateway_memory_extract_enabled,
    )

    monkeypatch.setenv("GATEWAY_ENHANCE_ENABLED", "true")
    monkeypatch.setenv("GATEWAY_MEMORY_EXTRACT_ENABLED", "true")
    monkeypatch.delenv("GATEWAY_MEMORY_ENABLED", raising=False)
    clear_flag_cache()
    assert is_gateway_memory_extract_enabled() is False


@pytest.mark.asyncio
async def test_async_select_does_not_run_pipeline_when_enhance_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    async def spy(env: Any) -> None:
        calls.append(env.model_group)

    monkeypatch.setattr("shared_quota_router.pipeline.run_pipeline", spy)
    strat = _strategy()
    kwargs = {
        "litellm_metadata": {"protocol": "anthropic_messages"},
        "messages": [{"role": "user", "content": "hi"}],
        "litellm_call_id": "call-off",
    }
    await strat.async_get_available_deployment(
        model=MODEL,
        messages=[{"role": "user", "content": "hi"}],
        request_kwargs=kwargs,
    )
    assert calls == []


@pytest.mark.asyncio
async def test_async_select_runs_pipeline_and_writes_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GATEWAY_ENHANCE_ENABLED", "true")
    clear_flag_cache()
    seen: dict[str, Any] = {}

    async def spy(env: Any) -> None:
        seen["model"] = env.model_group
        seen["quota"] = env.parent_quota_group_id
        seen["rid"] = env.parent_request_id
        env.messages.append({"role": "user", "content": "pipeline-mutated"})

    monkeypatch.setattr("shared_quota_router.pipeline.run_pipeline", spy)
    strat = _strategy()
    kwargs = {
        "litellm_metadata": {"protocol": "anthropic_messages"},
        "messages": [{"role": "user", "content": "hi"}],
        "litellm_call_id": "call-on",
    }
    named = [{"role": "user", "content": "hi"}]
    await strat.async_get_available_deployment(
        model=MODEL,
        messages=named,
        request_kwargs=kwargs,
    )
    assert seen["model"] == MODEL
    assert seen["quota"] == "volc-c"
    assert seen["rid"] == "call-on"
    assert kwargs["messages"][-1]["content"] == "pipeline-mutated"
    assert named[-1]["content"] == "pipeline-mutated"


@pytest.mark.asyncio
async def test_async_select_runs_pipeline_when_client_sets_internal_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GATEWAY_ENHANCE_ENABLED", "true")
    clear_flag_cache()
    calls: list[str] = []

    async def spy(env: Any) -> None:
        calls.append("ran")

    monkeypatch.setattr("shared_quota_router.pipeline.run_pipeline", spy)
    strat = _strategy()
    kwargs = {
        "litellm_metadata": {
            "protocol": "anthropic_messages",
            "internal_call": True,
        },
        "messages": [{"role": "user", "content": "hi"}],
        "litellm_call_id": "public-spoof-pipeline-envelope",
    }
    await strat.async_get_available_deployment(
        model=MODEL,
        messages=[{"role": "user", "content": "hi"}],
        request_kwargs=kwargs,
    )
    assert calls == ["ran"]


@pytest.mark.asyncio
async def test_async_select_skips_pipeline_for_trusted_internal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from shared_quota_router.internal_call import trusted_internal

    monkeypatch.setenv("GATEWAY_ENHANCE_ENABLED", "true")
    clear_flag_cache()
    calls: list[str] = []

    async def spy(env: Any) -> None:
        calls.append("ran")

    monkeypatch.setattr("shared_quota_router.pipeline.run_pipeline", spy)
    strat = _strategy()
    kwargs = {
        "litellm_metadata": {"protocol": "anthropic_messages"},
        "messages": [{"role": "user", "content": "hi"}],
        "litellm_call_id": "trusted-skip-pipeline-envelope",
    }
    with trusted_internal():
        await strat.async_get_available_deployment(
            model=MODEL,
            messages=[{"role": "user", "content": "hi"}],
            request_kwargs=kwargs,
        )
    assert calls == []
