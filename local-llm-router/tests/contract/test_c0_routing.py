"""C0 contract: CustomRoutingStrategyBase + mount + model_info readable.

Run:
  pip install 'litellm==1.90.5'
  pytest tests/contract/test_c0_routing.py -q

Or with submodule on PYTHONPATH:
  set PYTHONPATH=plugins;upstream/litellm
  pytest tests/contract/test_c0_routing.py -q
"""

from __future__ import annotations

import os
import sys

import pytest

# Ensure plugin package is importable
_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
_PLUGINS = os.path.join(_ROOT, "plugins")
_UPSTREAM = os.path.join(_ROOT, "upstream", "litellm")
for p in (_PLUGINS, _UPSTREAM):
    if p not in sys.path:
        sys.path.insert(0, p)

litellm = pytest.importorskip("litellm", reason="litellm required for C0 contract")
from litellm import Router  # noqa: E402

try:
    from litellm.router import CustomRoutingStrategyBase
except ImportError:
    from litellm.types.router import CustomRoutingStrategyBase  # type: ignore


from shared_quota_router.bootstrap import register  # noqa: E402
from shared_quota_router.lease import LeaseManager  # noqa: E402
from shared_quota_router.state_store import StateStore  # noqa: E402
from shared_quota_router.strategy import SharedQuotaRoutingStrategy  # noqa: E402


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
        keys = keys_and_args[:numkeys]
        args = keys_and_args[numkeys:]
        if numkeys == 3:
            status_key, inflight_key, lease_key = keys
            request_id = args[2]
            raw = self.data.get(status_key)
            if raw and any(s in raw for s in ('"EXHAUSTED"', '"DISABLED"', '"PROBING"')):
                return [0, "quota_unavailable"]
            inflight = self.incr(inflight_key)
            self.set(lease_key, request_id)
            return [1, str(inflight)]
        if numkeys == 2:
            inflight_key, lease_key = keys
            self.delete(lease_key)
            inflight = int(self.data.get(inflight_key, "0"))
            if inflight > 0:
                inflight = self.decr(inflight_key)
            return max(inflight, 0)
        return 0


def _model_list():
    return [
        {
            "model_name": "kimi-k3",
            "litellm_params": {
                "model": "openai/kimi-k3",
                "api_base": "https://example.invalid/a",
                "api_key": "fake-a",
            },
            "model_info": {
                "id": "opencode-a-kimi",
                "deployment_id": "opencode-a-kimi",
                "provider_id": "opencode-go",
                "quota_group_id": "opencode-a",
                "priority": 10,
            },
        },
        {
            "model_name": "kimi-k3",
            "litellm_params": {
                "model": "openai/kimi-k3",
                "api_base": "https://example.invalid/b",
                "api_key": "fake-b",
            },
            "model_info": {
                "id": "opencode-b-kimi",
                "deployment_id": "opencode-b-kimi",
                "provider_id": "opencode-go",
                "quota_group_id": "opencode-b",
                "priority": 20,
            },
        },
    ]


def test_c0_custom_routing_strategy_base_importable() -> None:
    assert CustomRoutingStrategyBase is not None
    # Duck-type: our strategy implements required methods
    mem = MemRedis()
    strat = SharedQuotaRoutingStrategy(store=StateStore(mem), lease_manager=LeaseManager(mem))
    assert callable(strat.async_get_available_deployment)
    assert callable(strat.get_available_deployment)


def test_c0_strategy_mounts_on_router() -> None:
    router = Router(model_list=_model_list(), set_verbose=False)
    mem = MemRedis()
    strat = SharedQuotaRoutingStrategy(
        store=StateStore(mem),
        lease_manager=LeaseManager(mem),
        router=router,
    )
    register(router, strategy=strat)
    # set_custom_routing_strategy replaces callables on the instance.
    # Bound-method identity is not stable; verify our selection runs.
    assert callable(router.get_available_deployment)
    assert callable(router.async_get_available_deployment)
    dep = router.get_available_deployment(
        model="kimi-k3",
        messages=[{"role": "user", "content": "hi"}],
        request_kwargs={"litellm_call_id": "c0-mount"},
    )
    assert dep["model_info"]["quota_group_id"] == "opencode-a"


def test_c0_model_info_custom_fields_readable() -> None:
    router = Router(model_list=_model_list(), set_verbose=False)
    mem = MemRedis()
    strat = SharedQuotaRoutingStrategy(
        store=StateStore(mem),
        lease_manager=LeaseManager(mem),
        router=router,
    )
    register(router, strategy=strat)

    deployment = router.get_available_deployment(
        model="kimi-k3",
        messages=[{"role": "user", "content": "hi"}],
        request_kwargs={"litellm_call_id": "c0-req-1", "metadata": {}},
    )
    assert deployment is not None
    info = deployment.get("model_info") or {}
    assert info.get("deployment_id") == "opencode-a-kimi"
    assert info.get("quota_group_id") == "opencode-a"
    assert info.get("priority") == 10
    assert info.get("provider_id") == "opencode-go"


@pytest.mark.asyncio
async def test_c0_async_get_available_deployment() -> None:
    router = Router(model_list=_model_list(), set_verbose=False)
    mem = MemRedis()
    strat = SharedQuotaRoutingStrategy(
        store=StateStore(mem),
        lease_manager=LeaseManager(mem),
        router=router,
    )
    register(router, strategy=strat)
    deployment = await router.async_get_available_deployment(
        model="kimi-k3",
        messages=[{"role": "user", "content": "hi"}],
        request_kwargs={"litellm_call_id": "c0-async-1"},
    )
    assert deployment["model_info"]["deployment_id"] == "opencode-a-kimi"
