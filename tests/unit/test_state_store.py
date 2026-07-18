from datetime import datetime, timedelta, timezone

import pytest

from shared_quota_router.models import QuotaGroup, QuotaGroupStatus
from shared_quota_router.state_store import StateStore, StateStoreError


class FakeRedis:
    def __init__(self) -> None:
        self.data: dict[str, str] = {}
        self.fail = False

    def get(self, name: str):
        if self.fail:
            raise ConnectionError("down")
        return self.data.get(name)

    def set(self, name: str, value, ex=None):
        if self.fail:
            raise ConnectionError("down")
        self.data[name] = value if isinstance(value, str) else str(value)
        return True

    def delete(self, *names: str):
        for n in names:
            self.data.pop(n, None)


def test_mark_exhausted_revision_and_status() -> None:
    store = StateStore(FakeRedis())
    g = QuotaGroup(
        quota_group_id="opencode-a",
        provider_id="opencode-go",
        account_id="opencode-a",
        display_name="A",
        revision=3,
    )
    store.put_quota_group(g)
    reset = datetime.now(timezone.utc) + timedelta(hours=1)
    out = store.mark_exhausted(
        "opencode-a",
        reason="five_hour_quota",
        reset_at=reset,
        expected_revision=3,
    )
    assert out.status == QuotaGroupStatus.EXHAUSTED
    assert out.revision == 4
    loaded = store.get_quota_group("opencode-a")
    assert loaded is not None
    assert loaded.status == QuotaGroupStatus.EXHAUSTED


def test_revision_conflict() -> None:
    store = StateStore(FakeRedis())
    g = QuotaGroup(
        quota_group_id="opencode-a",
        provider_id="opencode-go",
        account_id="a",
        display_name="A",
        revision=1,
    )
    store.put_quota_group(g)
    with pytest.raises(StateStoreError):
        store.mark_exhausted(
            "opencode-a",
            reason="x",
            expected_revision=0,
        )


def test_fail_closed_on_redis_down() -> None:
    r = FakeRedis()
    store = StateStore(r)
    r.fail = True
    with pytest.raises(StateStoreError):
        store.get_quota_group("opencode-a")
