"""Integration scenarios A–F (design §17.3) using in-process components + mock classifier path."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from shared_quota_router.callbacks import SharedQuotaCallback
from shared_quota_router.models import (
    Deployment,
    DeploymentRuntimeState,
    QuotaGroup,
    QuotaGroupStatus,
    RequestRoutingContext,
)
from shared_quota_router.registry import DeploymentRegistry
from shared_quota_router.state_store import StateStore
from shared_quota_router.strategy import NoAvailableDeploymentError, SharedQuotaSelector


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
            inflight_key, lease_key = keys[1], keys[2]
            self.incr(inflight_key)
            self.set(lease_key, args[2])
            return [1, "1"]
        if numkeys == 2:
            self.delete(keys[1])
            return 0
        return 0


def _stack():
    from shared_quota_router.lease import LeaseManager

    mem = MemRedis()
    store = StateStore(mem)
    lease = LeaseManager(mem)
    reg = DeploymentRegistry(
        [
            Deployment(
                deployment_id="a-kimi",
                model_group="kimi-k3",
                upstream_model="kimi-k3",
                provider_id="opencode-go",
                quota_group_id="a",
                priority=10,
            ),
            Deployment(
                deployment_id="a-glm",
                model_group="glm-5.2",
                upstream_model="glm-5.2",
                provider_id="opencode-go",
                quota_group_id="a",
                priority=10,
            ),
            Deployment(
                deployment_id="b-kimi",
                model_group="kimi-k3",
                upstream_model="kimi-k3",
                provider_id="opencode-go",
                quota_group_id="b",
                priority=20,
            ),
            Deployment(
                deployment_id="c-kimi",
                model_group="kimi-k3",
                upstream_model="kimi-k3",
                provider_id="volcengine",
                quota_group_id="c",
                priority=30,
            ),
        ]
    )
    sel = SharedQuotaSelector(reg, store, lease)
    cb = SharedQuotaCallback(store, lease)
    return sel, store, cb


def test_scenario_a_shared_quota_exhaust() -> None:
    sel, store, cb = _stack()
    cb.on_failure(
        {
            "litellm_call_id": "a1",
            "model_info": {
                "deployment_id": "a-kimi",
                "quota_group_id": "a",
                "provider_id": "opencode-go",
            },
            "response_status_code": 429,
            "exception": Exception("insufficient_quota exceeded quota"),
        },
        {
            "error": {
                "code": "insufficient_quota",
                "message": "You exceeded your current quota",
            }
        },
    )
    assert store.get_quota_group("a").status == QuotaGroupStatus.EXHAUSTED
    ctx = RequestRoutingContext(request_id="next")
    chosen = sel.select("kimi-k3", ctx)
    assert chosen.quota_group_id in {"b", "c"}
    # glm also cannot pick A
    with pytest.raises(NoAvailableDeploymentError):
        # only a has glm — if exhausted, no candidates
        sel.select("glm-5.2", RequestRoutingContext(request_id="glm1"))


def test_scenario_b_short_rate_limit() -> None:
    sel, store, cb = _stack()
    cb.on_failure(
        {
            "litellm_call_id": "b1",
            "model_info": {
                "deployment_id": "a-kimi",
                "quota_group_id": "a",
                "provider_id": "opencode-go",
            },
            "response_status_code": 429,
            "exception": Exception("Rate limit TPM"),
        },
        {"error": {"code": "rate_limit_exceeded", "message": "Rate limit reached for TPM"}},
    )
    g = store.get_quota_group("a")
    assert g is None or g.status == QuotaGroupStatus.AVAILABLE
    # glm still selectable on A
    glm = sel.select("glm-5.2", RequestRoutingContext(request_id="b2"))
    assert glm.deployment_id == "a-glm"


def test_scenario_c_auth() -> None:
    _, store, cb = _stack()
    cb.on_failure(
        {
            "litellm_call_id": "c1",
            "model_info": {
                "deployment_id": "a-kimi",
                "quota_group_id": "a",
                "provider_id": "opencode-go",
            },
            "response_status_code": 401,
            "exception": Exception("Invalid API key"),
        },
        {"error": {"message": "Invalid API key", "code": "invalid_api_key"}},
    )
    assert store.get_quota_group("a").status == QuotaGroupStatus.DISABLED


def test_scenario_d_provider_outage() -> None:
    from shared_quota_router.models import ProviderStatus

    sel, store, cb = _stack()
    cb.on_failure(
        {
            "litellm_call_id": "d1",
            "model_info": {
                "deployment_id": "a-kimi",
                "quota_group_id": "a",
                "provider_id": "opencode-go",
            },
            "response_status_code": 503,
            "exception": Exception("service unavailable"),
        },
        {"error": {"message": "unavailable"}},
    )
    # provider cooldown may be set
    # force both opencode groups unavailable via provider
    store.put_provider_status("opencode-go", ProviderStatus.COOLDOWN)
    chosen = sel.select("kimi-k3", RequestRoutingContext(request_id="d2"))
    assert chosen.quota_group_id == "c"


def test_scenario_e_recovery() -> None:
    from shared_quota_router.recovery_worker import RecoveryWorker

    sel, store, _ = _stack()
    store.put_quota_group(
        QuotaGroup(
            quota_group_id="a",
            provider_id="opencode-go",
            account_id="a",
            display_name="a",
            status=QuotaGroupStatus.EXHAUSTED,
            next_probe_at=datetime.now(timezone.utc) - timedelta(seconds=1),
        )
    )
    reg = DeploymentRegistry(
        [
            Deployment(
                deployment_id="a-kimi",
                model_group="kimi-k3",
                upstream_model="kimi-k3",
                provider_id="opencode-go",
                quota_group_id="a",
                priority=10,
            ),
            Deployment(
                deployment_id="a-glm",
                model_group="glm-5.2",
                upstream_model="glm-5.2",
                provider_id="opencode-go",
                quota_group_id="a",
                priority=10,
            ),
        ]
    )
    worker = RecoveryWorker(store, reg, redis=MemRedis(), probe_fn=lambda d: True)
    assert worker.run_probe_cycle(["a"])["a"] == "success"
    assert store.get_quota_group("a").status == QuotaGroupStatus.AVAILABLE
    assert sel.select("kimi-k3", RequestRoutingContext(request_id="e1")).quota_group_id == "a"
    assert sel.select("glm-5.2", RequestRoutingContext(request_id="e2")).quota_group_id == "a"


def test_scenario_f_stream_no_switch_after_first_byte() -> None:
    _, _, cb = _stack()
    kwargs: dict = {
        "litellm_call_id": "f1",
        "model_info": {
            "deployment_id": "a-kimi",
            "quota_group_id": "a",
            "provider_id": "opencode-go",
        },
        "metadata": {},
    }
    cb.mark_first_byte(kwargs)
    assert cb.should_allow_retry(kwargs) is False
    # failure still updates state for *next* request
    cb.on_failure(
        {
            **kwargs,
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
