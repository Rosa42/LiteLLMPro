"""Unit tests for shared-quota selection (no litellm required)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from shared_quota_router.lease import LeaseManager
from shared_quota_router.models import (
    Deployment,
    DeploymentRuntimeState,
    ProviderStatus,
    QuotaGroup,
    QuotaGroupStatus,
    RequestRoutingContext,
)
from shared_quota_router.registry import DeploymentRegistry
from shared_quota_router.state_store import StateStore, StateStoreError
from shared_quota_router.strategy import (
    NoAvailableDeploymentError,
    SharedQuotaSelector,
    session_key_from_request,
)


class MemRedis:
    def __init__(self) -> None:
        self.data: dict[str, str] = {}
        self.fail = False

    def get(self, name: str):
        if self.fail:
            raise ConnectionError("down")
        return self.data.get(name)

    def set(self, name: str, value, ex=None, nx=False):
        if self.fail:
            raise ConnectionError("down")
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
            status_key, inflight_key, lease_key = keys
            ttl, max_inflight, request_id = int(args[0]), int(args[1]), args[2]
            raw = self.data.get(status_key)
            if raw and any(s in raw for s in ('"EXHAUSTED"', '"DISABLED"', '"PROBING"')):
                return [0, "quota_unavailable"]
            inflight = int(self.data.get(inflight_key, "0"))
            if max_inflight > 0 and inflight >= max_inflight:
                return [0, "max_inflight"]
            inflight = self.incr(inflight_key)
            self.set(lease_key, request_id, ex=ttl)
            return [1, str(inflight)]
        if numkeys == 2:
            inflight_key, lease_key = keys
            self.delete(lease_key)
            inflight = int(self.data.get(inflight_key, "0"))
            if inflight > 0:
                inflight = self.decr(inflight_key)
            if inflight < 0:
                self.data[inflight_key] = "0"
                inflight = 0
            return inflight
        raise AssertionError("unexpected eval")


def _registry() -> DeploymentRegistry:
    reg = DeploymentRegistry(
        [
            Deployment(
                deployment_id="opencode-a-kimi",
                model_group="kimi-k3",
                upstream_model="openai/kimi-k3",
                provider_id="opencode-go",
                quota_group_id="opencode-a",
                priority=10,
            ),
            Deployment(
                deployment_id="opencode-a-glm",
                model_group="glm-5.2",
                upstream_model="openai/glm-5.2",
                provider_id="opencode-go",
                quota_group_id="opencode-a",
                priority=10,
            ),
            Deployment(
                deployment_id="opencode-b-kimi",
                model_group="kimi-k3",
                upstream_model="openai/kimi-k3",
                provider_id="opencode-go",
                quota_group_id="opencode-b",
                priority=20,
            ),
            Deployment(
                deployment_id="volc-c-kimi",
                model_group="kimi-k3",
                upstream_model="openai/kimi-k3",
                provider_id="volcengine",
                quota_group_id="volc-c",
                priority=30,
            ),
        ]
    )
    return reg


def _selector(r: MemRedis | None = None) -> tuple[SharedQuotaSelector, StateStore, MemRedis]:
    mem = r or MemRedis()
    store = StateStore(mem)
    lease = LeaseManager(mem)
    return SharedQuotaSelector(_registry(), store, lease), store, mem


def test_priority_fill_first_prefers_lower_priority() -> None:
    sel, _, _ = _selector()
    ctx = RequestRoutingContext(request_id="r1")
    chosen = sel.select("kimi-k3", ctx)
    assert chosen.deployment_id == "opencode-a-kimi"
    assert "opencode-a" in ctx.tried_quota_groups


def test_exhausted_quota_group_excluded() -> None:
    sel, store, _ = _selector()
    store.put_quota_group(
        QuotaGroup(
            quota_group_id="opencode-a",
            provider_id="opencode-go",
            account_id="a",
            display_name="A",
            status=QuotaGroupStatus.EXHAUSTED,
        )
    )
    ctx = RequestRoutingContext(request_id="r2")
    chosen = sel.select("kimi-k3", ctx)
    assert chosen.quota_group_id == "opencode-b"


def test_tried_quota_groups_not_reselected() -> None:
    sel, _, _ = _selector()
    ctx = RequestRoutingContext(request_id="r3")
    first = sel.select("kimi-k3", ctx)
    assert first.quota_group_id == "opencode-a"
    second = sel.select("kimi-k3", ctx)
    assert second.quota_group_id == "opencode-b"
    third = sel.select("kimi-k3", ctx)
    assert third.quota_group_id == "volc-c"
    with pytest.raises(NoAvailableDeploymentError):
        sel.select("kimi-k3", ctx)


def test_first_byte_blocks_selection() -> None:
    sel, _, _ = _selector()
    ctx = RequestRoutingContext(request_id="r4")
    ctx.mark_first_byte_sent()
    with pytest.raises(NoAvailableDeploymentError):
        sel.select("kimi-k3", ctx)


def test_dual_cooldown_deployment_not_quota_group() -> None:
    """Deployment cooldown on A/kimi must not block A/glm (model group ≠ quota group)."""
    sel, store, _ = _selector()
    store.put_deployment_state(
        DeploymentRuntimeState(
            deployment_id="opencode-a-kimi",
            is_in_cooldown=True,
            cooldown_until=datetime.now(timezone.utc) + timedelta(minutes=5),
        )
    )
    # A still AVAILABLE
    store.put_quota_group(
        QuotaGroup(
            quota_group_id="opencode-a",
            provider_id="opencode-go",
            account_id="a",
            display_name="A",
            status=QuotaGroupStatus.AVAILABLE,
        )
    )
    ctx = RequestRoutingContext(request_id="r5")
    # kimi should skip A/kimi, pick B
    kimi = sel.select("kimi-k3", ctx)
    assert kimi.deployment_id != "opencode-a-kimi"
    # fresh context for glm — A/glm still ok
    ctx2 = RequestRoutingContext(request_id="r5b")
    glm = sel.select("glm-5.2", ctx2)
    assert glm.deployment_id == "opencode-a-glm"


def test_redis_down_fail_closed() -> None:
    mem = MemRedis()
    sel, _, _ = _selector(mem)
    mem.fail = True
    ctx = RequestRoutingContext(request_id="r6")
    with pytest.raises(StateStoreError):
        sel.filter_candidates("kimi-k3", ctx)


def test_provider_cooldown_excludes() -> None:
    sel, store, _ = _selector()
    store.put_provider_status("opencode-go", ProviderStatus.COOLDOWN)
    ctx = RequestRoutingContext(request_id="r7")
    chosen = sel.select("kimi-k3", ctx)
    assert chosen.quota_group_id == "volc-c"


def test_affinity_preferred() -> None:
    sel, _, _ = _selector()
    ctx = RequestRoutingContext(request_id="r8")
    chosen = sel.select("kimi-k3", ctx, affinity_deployment_id="opencode-b-kimi")
    assert chosen.deployment_id == "opencode-b-kimi"


def test_session_key_from_metadata() -> None:
    h1 = session_key_from_request(
        model="kimi-k3",
        messages=None,
        request_kwargs={"metadata": {"session_id": "abc"}},
    )
    h2 = session_key_from_request(
        model="kimi-k3",
        messages=None,
        request_kwargs={"metadata": {"session_id": "abc"}},
    )
    assert h1 == h2
    h3 = session_key_from_request(
        model="kimi-k3",
        messages=None,
        request_kwargs={"metadata": {"session_id": "xyz"}},
    )
    assert h1 != h3
