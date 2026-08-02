from __future__ import annotations

from datetime import datetime, timedelta, timezone

from shared_quota_router.models import Deployment, QuotaGroup, QuotaGroupStatus
from shared_quota_router.recovery_worker import (
    RecoveryWorker,
    next_probe_delay,
    schedule_next_probe,
)
from shared_quota_router.registry import DeploymentRegistry
from shared_quota_router.state_store import StateStore


class MemRedis:
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


def test_backoff_sequence_and_cap() -> None:
    assert next_probe_delay(0) == 300
    assert next_probe_delay(1) == 900
    assert next_probe_delay(2) == 1800
    assert next_probe_delay(3) == 3600
    assert next_probe_delay(10) == 3600
    assert next_probe_delay(10) <= 7200


def test_no_fixed_five_hour_invention() -> None:
    """Without reset_at, next probe uses backoff minutes — not +5 hours as fact."""
    g = QuotaGroup(
        quota_group_id="a",
        provider_id="p",
        account_id="a",
        display_name="a",
        status=QuotaGroupStatus.EXHAUSTED,
        consecutive_failures=0,
        reset_at=None,
    )
    now = datetime(2026, 7, 18, 12, 0, tzinfo=timezone.utc)
    nxt = schedule_next_probe(g, now=now, probe_failed=True)
    assert nxt == now + timedelta(seconds=300)
    assert (nxt - now).total_seconds() != 5 * 3600


def test_probe_success_restores_group() -> None:
    store = StateStore(MemRedis())
    reg = DeploymentRegistry(
        [
            Deployment(
                deployment_id="a-kimi",
                model_group="kimi-k3",
                upstream_model="kimi-k3",
                provider_id="p",
                quota_group_id="a",
                priority=10,
                api_base="http://127.0.0.1:9",
            ),
            Deployment(
                deployment_id="a-glm",
                model_group="glm-5.2",
                upstream_model="glm-5.2",
                provider_id="p",
                quota_group_id="a",
                priority=10,
            ),
        ]
    )
    store.put_quota_group(
        QuotaGroup(
            quota_group_id="a",
            provider_id="p",
            account_id="a",
            display_name="a",
            status=QuotaGroupStatus.EXHAUSTED,
            next_probe_at=datetime.now(timezone.utc) - timedelta(seconds=1),
        )
    )
    worker = RecoveryWorker(store, reg, redis=MemRedis(), probe_fn=lambda d: True)
    result = worker.run_probe_cycle(["a"])
    assert result["a"] == "success"
    g = store.get_quota_group("a")
    assert g is not None and g.status == QuotaGroupStatus.AVAILABLE


def test_probe_fail_backoff() -> None:
    store = StateStore(MemRedis())
    reg = DeploymentRegistry(
        [
            Deployment(
                deployment_id="a-kimi",
                model_group="kimi-k3",
                upstream_model="kimi-k3",
                provider_id="p",
                quota_group_id="a",
                priority=10,
            )
        ]
    )
    store.put_quota_group(
        QuotaGroup(
            quota_group_id="a",
            provider_id="p",
            account_id="a",
            display_name="a",
            status=QuotaGroupStatus.EXHAUSTED,
            next_probe_at=datetime.now(timezone.utc) - timedelta(seconds=1),
        )
    )
    worker = RecoveryWorker(store, reg, redis=MemRedis(), probe_fn=lambda d: False)
    assert worker.run_probe_cycle(["a"])["a"] == "failed"
    g = store.get_quota_group("a")
    assert g is not None
    assert g.status == QuotaGroupStatus.EXHAUSTED
    assert g.next_probe_at is not None


def test_single_probe_lock() -> None:
    redis = MemRedis()
    store = StateStore(redis)
    reg = DeploymentRegistry([])
    worker = RecoveryWorker(store, reg, redis=redis, probe_fn=lambda d: True)
    assert worker.try_acquire_probe_lock("a") is True
    assert worker.try_acquire_probe_lock("a") is False
