"""B2 / Wave0: stream lifecycle wrapper and idempotent lease."""

from __future__ import annotations

import asyncio

import pytest

from shared_quota_router.lease import LeaseManager, lease_ttl_seconds
from shared_quota_router.stream_lifecycle import (
    ManagedStream,
    StreamLifecycleConfig,
    StreamLifecycleContext,
    StreamRenewFailedError,
    wrap_async_stream,
)


class FakeRedisLua:
    """Minimal redis-like with EVAL for acquire/release/renew scripts."""

    def __init__(self) -> None:
        self.data: dict[str, str] = {}

    def get(self, name: str):
        return self.data.get(name)

    def set(self, name: str, value, ex=None, nx=False):
        if nx and name in self.data:
            return False
        self.data[name] = str(value)
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
            if len(args) == 2:
                request_id, _ttl = args[0], int(args[1])
                current = self.data.get(lease_key)
                if not current or current != request_id:
                    return 0
                return 1
            request_id = args[0]
            current = self.data.get(lease_key)
            if not current or current != request_id:
                return int(self.data.get(inflight_key, "0"))
            self.delete(lease_key)
            inflight = int(self.data.get(inflight_key, "0"))
            if inflight > 0:
                inflight = self.decr(inflight_key)
            if inflight < 0:
                self.data[inflight_key] = "0"
                inflight = 0
            return inflight
        raise AssertionError("unexpected eval")


def test_lease_ttl_formula() -> None:
    assert lease_ttl_seconds(300) == 330


def test_acquire_release_inflight() -> None:
    lm = LeaseManager(FakeRedisLua())
    assert lm.acquire(quota_group_id="a", request_id="r1", request_timeout_seconds=60)
    assert lm.get_inflight("a") == 1
    assert lm.release(quota_group_id="a", request_id="r1") == 0
    assert lm.get_inflight("a") == 0


def test_acquire_blocked_when_exhausted() -> None:
    r = FakeRedisLua()
    r.data["sq:quota:a"] = '{"status": "EXHAUSTED"}'
    lm = LeaseManager(r)
    assert lm.acquire(quota_group_id="a", request_id="r1") is False


def test_release_wrong_request_id_does_not_decr() -> None:
    lm = LeaseManager(FakeRedisLua())
    assert lm.acquire(quota_group_id="a", request_id="r1")
    assert lm.get_inflight("a") == 1
    assert lm.release(quota_group_id="a", request_id="wrong") == 1
    assert lm.get_inflight("a") == 1
    assert lm.release(quota_group_id="a", request_id="r1") == 0


def test_double_release_is_idempotent() -> None:
    lm = LeaseManager(FakeRedisLua())
    assert lm.acquire(quota_group_id="a", request_id="r1")
    assert lm.release(quota_group_id="a", request_id="r1") == 0
    assert lm.release(quota_group_id="a", request_id="r1") == 0
    assert lm.get_inflight("a") == 0


def test_renew_requires_matching_lease() -> None:
    lm = LeaseManager(FakeRedisLua())
    assert lm.acquire(quota_group_id="a", request_id="r1", request_timeout_seconds=60)
    assert lm.renew(quota_group_id="a", request_id="r1", ttl_seconds=120) is True
    assert lm.renew(quota_group_id="a", request_id="other", ttl_seconds=120) is False


async def _async_gen(*chunks: str):
    for c in chunks:
        yield c


@pytest.mark.asyncio
async def test_managed_stream_passthrough_and_releases() -> None:
    lm = LeaseManager(FakeRedisLua())
    assert lm.acquire(quota_group_id="qg", request_id="req-1")
    flags = {"first_byte": False, "completed": False}

    ctx = StreamLifecycleContext(
        quota_group_id="qg",
        request_id="req-1",
        lease_manager=lm,
        config=StreamLifecycleConfig(
            request_timeout_seconds=60,
            absolute_max_seconds=60,
            renew_floor_seconds=3600,
        ),
        on_first_byte=lambda: flags.update(first_byte=True),
        on_stream_complete=lambda: flags.update(completed=True),
    )

    wrapped = wrap_async_stream(_async_gen("a", "b"), ctx)
    assert isinstance(wrapped, ManagedStream)
    chunks = [c async for c in wrapped]
    assert chunks == ["a", "b"]
    assert flags["first_byte"] is True
    assert flags["completed"] is True
    assert lm.get_inflight("qg") == 0
