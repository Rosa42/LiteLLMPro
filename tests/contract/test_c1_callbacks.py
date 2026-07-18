"""C1 contract: failure callback updates state; next selection avoids melted group."""

from __future__ import annotations

import os
import sys

import pytest

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
for p in (os.path.join(_ROOT, "plugins"), os.path.join(_ROOT, "upstream", "litellm")):
    if p not in sys.path:
        sys.path.insert(0, p)

pytest.importorskip("litellm")
from litellm import Router  # noqa: E402

from shared_quota_router.bootstrap import register  # noqa: E402
from shared_quota_router.callbacks import SharedQuotaCallback  # noqa: E402
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

    def eval(self, script, numkeys, *keys_and_args):
        keys = keys_and_args[:numkeys]
        args = keys_and_args[numkeys:]
        if numkeys == 3:
            self.incr(keys[1])
            self.set(keys[2], args[2])
            return [1, "1"]
        return 0


def _models():
    return [
        {
            "model_name": "kimi-k3",
            "litellm_params": {
                "model": "openai/kimi-k3",
                "api_base": "https://example.invalid/a",
                "api_key": "a",
            },
            "model_info": {
                "id": "a-kimi",
                "deployment_id": "a-kimi",
                "provider_id": "p",
                "quota_group_id": "a",
                "priority": 10,
            },
        },
        {
            "model_name": "kimi-k3",
            "litellm_params": {
                "model": "openai/kimi-k3",
                "api_base": "https://example.invalid/b",
                "api_key": "b",
            },
            "model_info": {
                "id": "b-kimi",
                "deployment_id": "b-kimi",
                "provider_id": "p",
                "quota_group_id": "b",
                "priority": 20,
            },
        },
    ]


def test_c1_failure_then_reroute() -> None:
    mem = MemRedis()
    store = StateStore(mem)
    lease = LeaseManager(mem)
    router = Router(model_list=_models(), set_verbose=False)
    strat = SharedQuotaRoutingStrategy(store=store, lease_manager=lease, router=router)
    register(router, strategy=strat)
    cb = SharedQuotaCallback(store=store, lease_manager=lease)

    first = router.get_available_deployment(
        model="kimi-k3",
        messages=[{"role": "user", "content": "hi"}],
        request_kwargs={"litellm_call_id": "c1-1"},
    )
    assert first["model_info"]["quota_group_id"] == "a"

    cb.on_failure(
        {
            "litellm_call_id": "c1-1",
            "model_info": first["model_info"],
            "response_status_code": 429,
            "exception": Exception("insufficient_quota exceeded"),
        },
        {
            "error": {
                "code": "insufficient_quota",
                "message": "You exceeded your current quota",
            }
        },
    )
    assert store.get_quota_group("a").status.value == "EXHAUSTED"

    second = router.get_available_deployment(
        model="kimi-k3",
        messages=[{"role": "user", "content": "hi"}],
        request_kwargs={"litellm_call_id": "c1-2"},
    )
    assert second["model_info"]["quota_group_id"] == "b"
    assert second["model_info"]["deployment_id"] == "b-kimi"
