"""P0 fixes: Redis reqctx, affinity clear on exhaust, first_byte hard gate."""

from __future__ import annotations

import pytest

from shared_quota_router.callbacks import SharedQuotaCallback
from shared_quota_router.models import Deployment, RequestRoutingContext
from shared_quota_router.registry import DeploymentRegistry
from shared_quota_router.state_store import StateStore
from shared_quota_router.strategy import (
    NoAvailableDeploymentError,
    SharedQuotaRoutingStrategy,
    SharedQuotaSelector,
    context_from_request_kwargs,
    save_request_context,
)


class MemRedis:
    def __init__(self) -> None:
        self.data: dict[str, str] = {}
        self.sets: dict[str, set[str]] = {}

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
            self.sets.pop(n, None)

    def sadd(self, name: str, *values):
        s = self.sets.setdefault(name, set())
        for v in values:
            s.add(str(v))
        return len(values)

    def smembers(self, name: str):
        return set(self.sets.get(name, set()))

    def expire(self, name: str, time: int):
        return True

    def incr(self, name: str):
        v = int(self.data.get(name, "0")) + 1
        self.data[name] = str(v)
        return v

    def decr(self, name: str):
        v = int(self.data.get(name, "0")) - 1
        self.data[name] = str(v)
        return v

    def eval(self, script, numkeys, *keys_and_args):
        keys = keys_and_args[:numkeys]
        args = keys_and_args[numkeys:]
        if numkeys == 3:
            self.incr(keys[1])
            self.set(keys[2], args[2])
            return [1, "1"]
        return 0


def test_reqctx_survives_new_kwargs_object() -> None:
    """P0-1: tried/first_byte stored by request_id, not kwargs identity."""
    store = StateStore(MemRedis())
    kwargs1: dict = {"litellm_call_id": "req-p0-1", "metadata": {}}
    ctx1 = context_from_request_kwargs(kwargs1, store=store)
    ctx1.mark_tried("opencode-a")
    ctx1.mark_first_byte_sent()
    save_request_context(ctx1, store)

    # Simulate LiteLLM retry with a brand-new kwargs dict
    kwargs2: dict = {"litellm_call_id": "req-p0-1", "metadata": {}}
    ctx2 = context_from_request_kwargs(kwargs2, store=store)
    assert "opencode-a" in ctx2.tried_quota_groups
    assert ctx2.first_byte_sent is True


def test_affinity_cleared_on_exhaust() -> None:
    """P0-2: mark_exhausted clears affinity index for quota group."""
    store = StateStore(MemRedis())
    store.set_affinity("sess-1", "opencode-a-kimi", quota_group_id="opencode-a")
    store.set_affinity("sess-2", "opencode-a-glm", quota_group_id="opencode-a")
    store.set_affinity("sess-b", "opencode-b-kimi", quota_group_id="opencode-b")

    assert store.get_affinity("sess-1") == "opencode-a-kimi"
    assert store.get_affinity("sess-b") == "opencode-b-kimi"

    store.mark_exhausted("opencode-a", reason="test")
    assert store.get_affinity("sess-1") is None
    assert store.get_affinity("sess-2") is None
    # other group untouched
    assert store.get_affinity("sess-b") == "opencode-b-kimi"


def test_affinity_cleared_on_auth_disable() -> None:
    store = StateStore(MemRedis())
    cb = SharedQuotaCallback(store=store)
    store.set_affinity("s1", "a-kimi", quota_group_id="opencode-a")
    cb.on_failure(
        {
            "litellm_call_id": "auth1",
            "model_info": {
                "deployment_id": "a-kimi",
                "quota_group_id": "opencode-a",
                "provider_id": "p",
            },
            "response_status_code": 401,
            "exception": Exception("Invalid API key"),
        },
        {"error": {"message": "Invalid API key", "code": "invalid_api_key"}},
    )
    assert store.get_affinity("s1") is None


def test_first_byte_blocks_strategy_selection() -> None:
    """P0-3: strategy raises when Redis says first_byte_sent."""
    mem = MemRedis()
    store = StateStore(mem)
    reg = DeploymentRegistry(
        [
            Deployment(
                deployment_id="a-kimi",
                model_group="kimi-k3",
                upstream_model="kimi-k3",
                provider_id="p",
                quota_group_id="a",
                priority=10,
            ),
            Deployment(
                deployment_id="b-kimi",
                model_group="kimi-k3",
                upstream_model="kimi-k3",
                provider_id="p",
                quota_group_id="b",
                priority=20,
            ),
        ]
    )
    model_list = [
        {
            "model_name": "kimi-k3",
            "litellm_params": {"model": "openai/kimi-k3", "api_key": "a"},
            "model_info": {
                "id": "a-kimi",
                "deployment_id": "a-kimi",
                "quota_group_id": "a",
                "provider_id": "p",
                "priority": 10,
            },
        },
        {
            "model_name": "kimi-k3",
            "litellm_params": {"model": "openai/kimi-k3", "api_key": "b"},
            "model_info": {
                "id": "b-kimi",
                "deployment_id": "b-kimi",
                "quota_group_id": "b",
                "provider_id": "p",
                "priority": 20,
            },
        },
    ]

    class FakeRouter:
        def __init__(self):
            self.model_list = model_list

    strat = SharedQuotaRoutingStrategy(
        store=store, lease_manager=None, router=FakeRouter(), registry=reg
    )
    cb = SharedQuotaCallback(store=store)

    kwargs: dict = {"litellm_call_id": "stream-1", "metadata": {}}
    # first selection ok
    dep = strat.get_available_deployment(
        model="kimi-k3",
        messages=[{"role": "user", "content": "hi"}],
        request_kwargs=kwargs,
    )
    assert dep["model_info"]["quota_group_id"] == "a"

    # stream starts
    cb.mark_first_byte(kwargs)

    # retry with NEW kwargs but same call id → must refuse
    kwargs_retry: dict = {"litellm_call_id": "stream-1", "metadata": {}}
    with pytest.raises(NoAvailableDeploymentError, match="first byte"):
        strat.get_available_deployment(
            model="kimi-k3",
            messages=[{"role": "user", "content": "hi"}],
            request_kwargs=kwargs_retry,
        )


def test_tried_across_retry_without_shared_kwargs() -> None:
    mem = MemRedis()
    store = StateStore(mem)
    reg = DeploymentRegistry(
        [
            Deployment(
                deployment_id="a-kimi",
                model_group="kimi-k3",
                upstream_model="kimi-k3",
                provider_id="p",
                quota_group_id="a",
                priority=10,
            ),
            Deployment(
                deployment_id="b-kimi",
                model_group="kimi-k3",
                upstream_model="kimi-k3",
                provider_id="p",
                quota_group_id="b",
                priority=20,
            ),
        ]
    )
    sel = SharedQuotaSelector(reg, store, lease_manager=None)
    ctx = RequestRoutingContext(request_id="retry-1")
    first = sel.select("kimi-k3", ctx)
    assert first.quota_group_id == "a"
    save_request_context(ctx, store)

    # new context object loaded from redis
    ctx2 = context_from_request_kwargs(
        {"litellm_call_id": "retry-1"},
        store=store,
    )
    second = sel.select("kimi-k3", ctx2)
    assert second.quota_group_id == "b"
